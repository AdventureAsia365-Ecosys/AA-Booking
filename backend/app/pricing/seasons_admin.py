"""Pricing > Seasons and Pricing > Holiday events (AA-1091, bk-055; root ADR 0005b decision 4): staff data with
maker-checker, like the other pricing data.

- **Calendars** (``booking.season_calendars``): one per destination (AA's sell-side seasons, the calendar a trip plan
  uses) or per supplier contract (the supplier's seasons; stored for the cost sheet of a later step). Creating an
  empty calendar changes no price (every date takes its default season, ``low``); it is a direct, audited write.
  A change of the default season is proposed with a reason and approved (or rejected, with a reason) by a second
  person.
- **Ranges** (``booking.season_ranges``): a season range (low, high, peak, festive) with fixed dates, a festive range
  that names a holiday event (country + event code, days before and after), or a stop-sale range. Each is proposed
  (pending, with a required reason, the ``note``) and counts once a second person approves it; the maker may
  withdraw it while pending, with a reason. Removing an approved range is a request with a reason that a second
  person approves (or rejects, with a reason). Two fixed-date season ranges of one calendar never overlap:
  a proposal or an approval that would make them is refused (409 ``range_overlaps``), checked under a lock of the
  calendar row so two approvals cannot both pass.
- **Holiday events** (``booking.holiday_events``): one row per country, event code and year, the same maker-checker
  and reasons.
  No holiday date is ever written in code; a festive range takes its dates from the event row of each year.
- **The calendar of a trip plan** (``trip_plans.season_calendar_id``): :func:`assign_default_calendars` (run by the
  catalog sync) gives every plan without one the calendar of the destination of its first day, creating that empty
  calendar when it is missing. A staff change is a request with a reason (:func:`request_plan_calendar`) that a
  second person approves or rejects (:func:`decide_plan_calendar`): the calendar drives the plan's peak uplift,
  festive quote validity and stop-sale, so it gets the maker-checker the ranges have.

Permissions: reads ``pricing.read``; proposals ``season.write``; approvals ``season.approve`` (never one's own
proposal: 403 ``maker_checker``, and the table CHECKs). A reason (at most 300 characters, ADR 0005b d1, the bk-048
model) is required on every proposal, withdrawal, removal request and rejection; it is stored on the row when the row
keeps it (``note``, ``reason``, ``withdraw_reason``, ``end_reason``, ``reject_reason``, ``default_request_reason``,
``season_calendar_request_reason``) and always in the audit row (a rejected removal or default change clears its
request). Every write is audited. Makers and checkers are returned as people (``{id, name}``, :mod:`app.auth.people`).
Inputs and outputs use inclusive end dates; the tables keep half-open ranges (d10).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import asyncpg

from app import audit
from app.auth import people
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.pricing import seasons

ONE_DAY = timedelta(days=1)
REASON_MAX = 300
NAME_MAX = 120

CALENDARS_SQL = """
SELECT c.id::text AS id, c.owner_kind, c.destination_id::text AS destination_id,
       c.supplier_contract_id::text AS supplier_contract_id, c.name, c.default_season, c.pending_default_season,
       c.default_requested_by, c.default_requested_at, c.default_request_reason, c.created_by,
       c.created_at, c.updated_at,
       (SELECT count(*) FROM booking.season_ranges r WHERE r.calendar_id = c.id AND r.status = 'approved'
          AND r.ended_at IS NULL) AS ranges,
       (SELECT count(*) FROM booking.season_ranges r WHERE r.calendar_id = c.id
          AND (r.status = 'pending' OR (r.end_requested_at IS NOT NULL AND r.ended_at IS NULL))) AS waiting,
       (SELECT count(*) FROM booking.trip_plans p WHERE p.season_calendar_id = c.id) AS plans
FROM booking.season_calendars c
"""
RANGES_SQL = """
SELECT r.id::text AS id, r.calendar_id::text AS calendar_id, r.kind, r.season_code, r.date_from, r.date_to,
       r.holiday_country::text AS holiday_country, r.holiday_code, r.days_before, r.days_after, r.note, r.status,
       r.created_by, r.created_at, r.decided_by, r.decided_at, r.reject_reason, r.withdraw_reason, r.end_requested_by,
       r.end_requested_at, r.end_reason, r.ended_by, r.ended_at
FROM booking.season_ranges r
"""
EVENTS_SQL = """
SELECT e.id::text AS id, e.country_iso2::text AS country_iso2, e.code, e.name, e.year, e.date_from, e.date_to,
       e.reason, e.status, e.created_by, e.created_at, e.decided_by, e.decided_at, e.reject_reason, e.withdraw_reason,
       e.end_requested_by, e.end_requested_at, e.end_reason, e.ended_by, e.ended_at,
       (SELECT count(DISTINCT r.calendar_id) FROM booking.season_ranges r
         WHERE r.holiday_country = e.country_iso2 AND r.holiday_code = e.code AND r.status IN ('pending', 'approved')
           AND r.ended_at IS NULL) AS calendars
FROM booking.holiday_events e
"""


# ── Shapes ───────────────────────────────────────────────────────────────────


def _last(end: date | None) -> date | None:
    return None if end is None else end - ONE_DAY


def state(row: Any) -> str:
    """pending | approved | rejected | withdrawn | ending (removal asked) | ended."""
    if row["ended_at"] is not None:
        return "ended"
    if row["end_requested_at"] is not None:
        return "ending"
    return str(row["status"])


def range_out(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["date_to"] = _last(row["date_to"])
    out["state"] = state(row)
    out["holiday_country"] = (row["holiday_country"] or "").strip() or None
    return out


def event_out(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["date_to"] = _last(row["date_to"])
    out["state"] = state(row)
    out["country_iso2"] = str(row["country_iso2"]).strip()
    out["calendars"] = int(row["calendars"])
    return out


def calendar_out(row: Any) -> dict[str, Any]:
    out = dict(row)
    for k in ("ranges", "waiting", "plans"):
        out[k] = int(row[k])
    return out


CALENDAR_PEOPLE = ("created_by", "default_requested_by")
CHANGE_PEOPLE = ("created_by", "decided_by", "end_requested_by", "ended_by")


async def _people(conn: asyncpg.Connection, items: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict]:
    await people.resolve(conn, items, fields)
    return items


async def _range_out(conn: asyncpg.Connection, range_id: str) -> dict[str, Any]:
    row = await conn.fetchrow(RANGES_SQL + " WHERE r.id = $1::uuid", range_id)
    return (await _people(conn, [range_out(row)], CHANGE_PEOPLE))[0]


async def _event_out(conn: asyncpg.Connection, event_id: str) -> dict[str, Any]:
    row = await conn.fetchrow(EVENTS_SQL + " WHERE e.id = $1::uuid", event_id)
    return (await _people(conn, [event_out(row)], CHANGE_PEOPLE))[0]


# ── Permissions ──────────────────────────────────────────────────────────────


def _read(staff: Staff) -> None:
    if not staff.can("pricing.read"):
        raise Forbidden("forbidden", "seasons are pricing data (pricing.read)")


def _write(staff: Staff) -> None:
    if not staff.can("season.write"):
        raise Forbidden("forbidden", "changing seasons or holiday events needs season.write")


def _approve(staff: Staff, maker: str | None) -> None:
    if not staff.can("season.approve"):
        raise Forbidden("forbidden", "approving seasons or holiday events needs season.approve")
    if maker == staff.cis_user_id:
        raise Forbidden("maker_checker", "a second person approves or rejects it")


def _text(value: str | None, field: str, limit: int, *, required: bool = False) -> str | None:
    text = (value or "").strip() or None
    if text is None and required:
        raise Invalid(f"{field}_required", f"give a {field.replace('_', ' ')}")
    if text is not None and len(text) > limit:
        raise Invalid(f"{field}_too_long", f"the {field.replace('_', ' ')} is at most {limit} characters")
    return text


async def _audit(conn: asyncpg.Connection, staff: Staff, action: str, kind: str, ident: str, after: Any) -> None:
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=action,
        entity_type=kind,
        entity_id=ident,
        after=json.loads(json.dumps(after, default=str)),
        entity_label=action.split(".")[0].replace("_", " "),
        summary=action.replace("_", " ").replace(".", ": "),
    )


# ── Calendars ────────────────────────────────────────────────────────────────


async def list_calendars(conn: asyncpg.Connection, staff: Staff, owner_kind: str | None = None) -> dict[str, Any]:
    _read(staff)
    rows = await conn.fetch(
        CALENDARS_SQL + " WHERE ($1::text IS NULL OR c.owner_kind = $1) ORDER BY c.owner_kind, c.name", owner_kind
    )
    items = await _people(conn, [calendar_out(r) for r in rows], CALENDAR_PEOPLE)
    return {"items": items, "season_codes": list(seasons.SEASON_CODES)}


async def _calendar(conn: asyncpg.Connection, calendar_id: str, *, lock: bool = False) -> asyncpg.Record:
    try:
        if lock:
            await conn.execute("SELECT 1 FROM booking.season_calendars WHERE id = $1::uuid FOR UPDATE", calendar_id)
        row = await conn.fetchrow(CALENDARS_SQL + " WHERE c.id = $1::uuid", calendar_id)
    except asyncpg.DataError:
        row = None
    if row is None:
        raise NotFound("calendar_not_found", "no such season calendar")
    return row


async def get_calendar(conn: asyncpg.Connection, staff: Staff, calendar_id: str, year: int | None) -> dict[str, Any]:
    """A calendar, its ranges (every state) and, for ``year``, its resolved season segments (what prices use)."""
    _read(staff)
    row = await _calendar(conn, calendar_id)
    ranges = await conn.fetch(RANGES_SQL + " WHERE r.calendar_id = $1::uuid ORDER BY r.created_at DESC", calendar_id)
    out: dict[str, Any] = {
        **(await _people(conn, [calendar_out(row)], CALENDAR_PEOPLE))[0],
        "ranges": await _people(conn, [range_out(r) for r in ranges], CHANGE_PEOPLE),
    }
    if year is not None:
        cal = (await seasons.load_many(conn, [calendar_id]))[calendar_id]
        start, end = date(year, 1, 1), date(year + 1, 1, 1)
        out["year"] = year
        out["segments"] = [{"from": s.start, "to": s.end - ONE_DAY, "season": s.code} for s in cal.segments(start, end)]
        out["stop_sales"] = [
            {"from": max(s.start, start), "to": min(s.end, end) - ONE_DAY}
            for s in cal.stop_sales
            if s.start < end and start < s.end
        ]
    return out


async def create_calendar(conn: asyncpg.Connection, staff: Staff, body: dict[str, Any]) -> dict[str, Any]:
    """An empty calendar of a destination or a supplier contract (409 ``calendar_exists`` when it has one)."""
    _write(staff)
    kind = body.get("owner_kind")
    dest, contract = body.get("destination_id"), body.get("supplier_contract_id")
    if (
        kind not in ("destination", "supplier_contract")
        or (kind == "destination") != bool(dest)
        or (kind == "supplier_contract") != bool(contract)
    ):
        raise Invalid("invalid_owner", "a calendar belongs to one destination or one supplier contract")
    name = _text(body.get("name"), "name", NAME_MAX, required=True)
    if contract and not await conn.fetchval(
        "SELECT 1 FROM booking.supplier_contracts WHERE id = $1::uuid", str(contract)
    ):
        raise Invalid("contract_not_found", "no such supplier contract")
    ident = await conn.fetchval(
        "INSERT INTO booking.season_calendars (owner_kind, destination_id, supplier_contract_id, name, created_by)"
        " VALUES ($1, $2::uuid, $3::uuid, $4, $5) ON CONFLICT DO NOTHING RETURNING id::text",
        kind,
        str(dest) if dest else None,
        str(contract) if contract else None,
        name,
        staff.cis_user_id,
    )
    if ident is None:
        raise Conflict("calendar_exists", "this owner already has a season calendar")
    await _audit(conn, staff, "season_calendar.created", "season_calendar", ident, {"name": name, "owner": kind})
    return await get_calendar(conn, staff, ident, None)


async def propose_default(
    conn: asyncpg.Connection, staff: Staff, calendar_id: str, code: str, reason: str | None
) -> dict[str, Any]:
    _write(staff)
    row = await _calendar(conn, calendar_id, lock=True)
    if code not in seasons.SEASON_CODES:
        raise Invalid("invalid_season", f"a season is one of {', '.join(seasons.SEASON_CODES)}")
    text = _text(reason, "reason", REASON_MAX, required=True)
    if row["pending_default_season"] is not None:
        raise Conflict("default_pending", "a change of the default season waits for approval")
    await conn.execute(
        "UPDATE booking.season_calendars SET pending_default_season = $2, default_requested_by = $3,"
        " default_requested_at = now(), default_request_reason = $4, updated_at = now() WHERE id = $1::uuid",
        calendar_id,
        code,
        staff.cis_user_id,
        text,
    )
    await _audit(
        conn, staff, "season_calendar.default_proposed", "season_calendar", calendar_id, {"to": code, "reason": text}
    )
    return await get_calendar(conn, staff, calendar_id, None)


async def decide_default(
    conn: asyncpg.Connection, staff: Staff, calendar_id: str, *, approve: bool, reason: str | None = None
) -> dict[str, Any]:
    """Approve or reject (with a required reason, kept in the audit row: the request is cleared) a default change."""
    row = await _calendar(conn, calendar_id, lock=True)
    if row["pending_default_season"] is None:
        raise Conflict("nothing_pending", "no change of the default season waits")
    _approve(staff, row["default_requested_by"])
    text = _text(reason, "reason", REASON_MAX, required=not approve)
    await conn.execute(
        "UPDATE booking.season_calendars SET default_season = CASE WHEN $2 THEN pending_default_season"
        " ELSE default_season END, pending_default_season = NULL, default_requested_by = NULL,"
        " default_requested_at = NULL, default_request_reason = NULL, updated_at = now() WHERE id = $1::uuid",
        calendar_id,
        approve,
    )
    action = "season_calendar.default_approved" if approve else "season_calendar.default_rejected"
    await _audit(
        conn,
        staff,
        action,
        "season_calendar",
        calendar_id,
        {"to": row["pending_default_season"], "request_reason": row["default_request_reason"], "reason": text},
    )
    return await get_calendar(conn, staff, calendar_id, None)


# ── Ranges ───────────────────────────────────────────────────────────────────

LIVE_FIXED_SQL = """
SELECT r.id::text AS id, r.date_from, r.date_to, r.season_code, r.status FROM booking.season_ranges r
WHERE r.calendar_id = $1::uuid AND r.kind = 'season' AND r.date_from IS NOT NULL AND r.ended_at IS NULL
  AND r.status IN ('pending', 'approved') AND r.id <> coalesce($2::uuid, '00000000-0000-0000-0000-000000000000')
"""


async def _no_overlap(conn: asyncpg.Connection, calendar_id: str, span: seasons.Span, skip: str | None) -> None:
    """Refuse a fixed-date season range that would overlap a live (pending or approved) one of the calendar."""
    live = [seasons.Span(r["date_from"], r["date_to"], r["season_code"], r["id"])
            for r in await conn.fetch(LIVE_FIXED_SQL, calendar_id, skip)]  # fmt: skip
    clash = seasons.overlapping(span, live)
    if clash:
        first = clash[0]
        raise Conflict(
            "range_overlaps",
            f"these dates overlap the {first.code} season {first.start.isoformat()} to "
            f"{(first.end - ONE_DAY).isoformat()} of this calendar",
            range_id=first.range_id,
        )


async def propose_range(conn: asyncpg.Connection, staff: Staff, calendar_id: str, body: dict[str, Any]) -> dict:
    _write(staff)
    await _calendar(conn, calendar_id, lock=True)
    kind = body.get("kind") or "season"
    code = body.get("season_code") if kind == "season" else None
    if kind not in ("season", "stop_sale") or (kind == "season" and code not in seasons.SEASON_CODES):
        raise Invalid("invalid_range", "a range is a season (low, high, peak, festive) or a stop-sale")
    event = (
        (body.get("holiday_country") or "").strip().upper() or None,
        (body.get("holiday_code") or "").strip() or None,
    )
    first, last = body.get("date_from"), body.get("date_to")
    note = _text(body.get("note"), "note", REASON_MAX, required=True)
    if event[0] or event[1]:
        if code != "festive" or not (event[0] and event[1]) or first or last:
            raise Invalid("invalid_range", "only a festive range names a holiday event, and then it has no dates")
        before, after = int(body.get("days_before") or 0), int(body.get("days_after") or 0)
        if not (0 <= before <= 60 and 0 <= after <= 60):  # noqa: PLR2004 — the table CHECK's bound
            raise Invalid("invalid_days", "days before and after are 0 to 60")
        dates: tuple[date | None, date | None] = (None, None)
    else:
        if not first or not last or last < first:
            raise Invalid("invalid_dates", "give the first and the last date of the range")
        dates = (first, last + ONE_DAY)
        before = after = 0
        if (dates[1] - first).days > 366:  # noqa: PLR2004 — the table CHECK's bound
            raise Invalid("invalid_dates", "a range is at most one year; add another range for the next year")
        if kind == "season":
            await _no_overlap(conn, calendar_id, seasons.Span(first, dates[1], code), None)
    ident = await conn.fetchval(
        "INSERT INTO booking.season_ranges (calendar_id, kind, season_code, date_from, date_to, holiday_country,"
        " holiday_code, days_before, days_after, note, created_by) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9,"
        " $10, $11) RETURNING id::text",
        calendar_id,
        kind,
        code,
        dates[0],
        dates[1],
        event[0],
        event[1],
        before,
        after,
        note,
        staff.cis_user_id,
    )
    await _audit(conn, staff, "season_range.proposed", "season_range", ident, {**body, "calendar_id": calendar_id})
    return await _range_out(conn, ident)


async def _range(conn: asyncpg.Connection, range_id: str) -> asyncpg.Record:
    try:
        row = await conn.fetchrow(RANGES_SQL + " WHERE r.id = $1::uuid", range_id)
    except asyncpg.DataError:
        row = None
    if row is None:
        raise NotFound("range_not_found", "no such season range")
    await _calendar(conn, row["calendar_id"], lock=True)
    return await conn.fetchrow(RANGES_SQL + " WHERE r.id = $1::uuid", range_id)


async def decide_range(
    conn: asyncpg.Connection, staff: Staff, range_id: str, action: str, reason: str | None = None
) -> dict[str, Any]:
    """``approve`` / ``reject`` (checker), ``withdraw`` (maker, pending), ``end`` (ask to remove an approved range),
    ``end_approve`` / ``end_reject`` (checker of the removal). A reason is required for all but the two approvals."""
    text = _reason_for(action, reason)
    row = await _range(conn, range_id)
    table = "booking.season_ranges"
    if action in ("approve", "reject"):
        if row["status"] != "pending":
            raise Conflict("not_pending", f"the range is {state(row)}")
        _approve(staff, row["created_by"])
        if action == "approve" and row["kind"] == "season" and row["date_from"] is not None:
            await _no_overlap(
                conn, row["calendar_id"], seasons.Span(row["date_from"], row["date_to"], row["season_code"]), range_id
            )
        await _decide(conn, table, range_id, staff, action, text)
    elif action == "withdraw":
        _write(staff)
        if row["status"] != "pending" or row["created_by"] != staff.cis_user_id:
            raise Conflict("not_withdrawable", "only its maker withdraws a pending range")
        await _decide(conn, table, range_id, staff, action, text)
    else:
        await _end(conn, table, row, staff, action, text)
    await _audit(conn, staff, f"season_range.{action}", "season_range", range_id, _decision_audit(row, text))
    return await _range_out(conn, range_id)


# Decisions that need no reason: the checker agrees with the maker's (which the row already holds).
NO_REASON = ("approve", "end_approve")


def _reason_for(action: str, reason: str | None) -> str | None:
    return _text(reason, "reason", REASON_MAX, required=action not in NO_REASON)


def _decision_audit(row: Any, reason: str | None) -> dict[str, Any]:
    """The audit of a decision: its reason and the request reason it answers (a rejected removal clears it)."""
    return {"reason": reason, "end_reason": row["end_reason"]}


async def _decide(
    conn: asyncpg.Connection, table: str, ident: str, staff: Staff, action: str, reason: str | None
) -> None:
    status = {"approve": "approved", "reject": "rejected", "withdraw": "withdrawn"}[action]
    await conn.execute(
        f"UPDATE {table} SET status = $2, decided_by = $3, decided_at = now(), reject_reason = $4,"  # noqa: S608
        " withdraw_reason = $5 WHERE id = $1::uuid",
        ident,
        status,
        staff.cis_user_id,
        reason if action == "reject" else None,
        reason if action == "withdraw" else None,
    )


async def _end(conn: asyncpg.Connection, table: str, row: Any, staff: Staff, action: str, reason: str | None) -> None:
    if row["status"] != "approved" or row["ended_at"] is not None:
        raise Conflict("not_in_force", f"it is {state(row)}")
    if action == "end":
        _write(staff)
        if row["end_requested_at"] is not None:
            raise Conflict("end_pending", "its removal already waits for approval")
        await conn.execute(
            f"UPDATE {table} SET end_requested_by = $2, end_requested_at = now(), end_reason = $3"  # noqa: S608
            " WHERE id = $1::uuid",
            row["id"],
            staff.cis_user_id,
            reason,
        )
        return
    if action not in ("end_approve", "end_reject"):
        raise Invalid("invalid_action", action)
    if row["end_requested_at"] is None:
        raise Conflict("nothing_pending", "no removal waits for approval")
    _approve(staff, row["end_requested_by"])
    if action == "end_reject":
        sql = (
            f"UPDATE {table} SET end_requested_by = NULL, end_requested_at = NULL, end_reason = NULL"  # noqa: S608
            " WHERE id = $1::uuid"
        )
        await conn.execute(sql, row["id"])
        return
    sql = f"UPDATE {table} SET ended_by = $2, ended_at = now() WHERE id = $1::uuid"  # noqa: S608
    await conn.execute(sql, row["id"], staff.cis_user_id)


# ── Holiday events ───────────────────────────────────────────────────────────


async def list_events(conn: asyncpg.Connection, staff: Staff, country: str | None, year: int | None) -> dict[str, Any]:
    _read(staff)
    rows = await conn.fetch(
        EVENTS_SQL + " WHERE ($1::text IS NULL OR e.country_iso2 = $1) AND ($2::int IS NULL OR e.year = $2)"
        " ORDER BY e.date_from, e.country_iso2, e.code",
        (country or "").upper() or None,
        year,
    )
    countries = await conn.fetch("SELECT iso2::text AS iso2, name FROM booking.countries ORDER BY name")
    return {
        "items": await _people(conn, [event_out(r) for r in rows], CHANGE_PEOPLE),
        "countries": [{"iso2": c["iso2"].strip(), "name": c["name"]} for c in countries],
    }


async def propose_event(conn: asyncpg.Connection, staff: Staff, body: dict[str, Any]) -> dict[str, Any]:
    _write(staff)
    country = (body.get("country_iso2") or "").strip().upper()
    code = (body.get("code") or "").strip().lower()
    name = _text(body.get("name"), "name", NAME_MAX, required=True)
    reason = _text(body.get("reason"), "reason", REASON_MAX, required=True)
    first, last = body.get("date_from"), body.get("date_to")
    if not first or not last or last < first:
        raise Invalid("invalid_dates", "give the first and the last day of the event")
    if (last - first).days >= 60:  # noqa: PLR2004 — the table CHECK's bound
        raise Invalid("invalid_dates", "an event lasts at most 60 days")
    if not await conn.fetchval("SELECT 1 FROM booking.countries WHERE iso2 = $1", country):
        raise Invalid("invalid_country", "pick a country of the list")
    try:
        ident = await conn.fetchval(
            "INSERT INTO booking.holiday_events (country_iso2, code, name, year, date_from, date_to, reason,"
            " created_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id::text",
            country,
            code,
            name,
            first.year,
            first,
            last + ONE_DAY,
            reason,
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError:
        raise Conflict("event_exists", f"{country} {code} {first.year} already exists (pending or approved)") from None
    except asyncpg.CheckViolationError:
        raise Invalid("invalid_event", "the code is lower case letters, digits and _ (2 to 40)") from None
    await _audit(conn, staff, "holiday_event.proposed", "holiday_event", ident, body)
    return await _event_out(conn, ident)


async def decide_event(
    conn: asyncpg.Connection, staff: Staff, event_id: str, action: str, reason: str | None = None
) -> dict[str, Any]:
    text = _reason_for(action, reason)
    try:
        row = await conn.fetchrow(EVENTS_SQL + " WHERE e.id = $1::uuid FOR UPDATE OF e", event_id)
    except asyncpg.DataError:
        row = None
    if row is None:
        raise NotFound("event_not_found", "no such holiday event")
    table = "booking.holiday_events"
    if action in ("approve", "reject"):
        if row["status"] != "pending":
            raise Conflict("not_pending", f"the event is {state(row)}")
        _approve(staff, row["created_by"])
        await _decide(conn, table, event_id, staff, action, text)
    elif action == "withdraw":
        _write(staff)
        if row["status"] != "pending" or row["created_by"] != staff.cis_user_id:
            raise Conflict("not_withdrawable", "only its maker withdraws a pending event")
        await _decide(conn, table, event_id, staff, action, text)
    else:
        await _end(conn, table, row, staff, action, text)
    await _audit(conn, staff, f"holiday_event.{action}", "holiday_event", event_id, _decision_audit(row, text))
    return await _event_out(conn, event_id)


# ── The calendar of a trip plan ──────────────────────────────────────────────


PLAN_CALENDAR_SQL = """
SELECT p.id::text AS id, p.season_calendar_id::text AS calendar_id, c.name AS calendar_name,
       p.pending_season_calendar_id::text AS pending_calendar_id, pc.name AS pending_calendar_name,
       p.season_calendar_requested_by AS requested_by, p.season_calendar_requested_at AS requested_at,
       p.season_calendar_request_reason AS request_reason
FROM booking.trip_plans p
LEFT JOIN booking.season_calendars c ON c.id = p.season_calendar_id
LEFT JOIN booking.season_calendars pc ON pc.id = p.pending_season_calendar_id
WHERE p.source_system = 'cis' AND p.source_id = $1
"""


async def _plan_calendar(conn: asyncpg.Connection, source_id: str, *, lock: bool = False) -> asyncpg.Record:
    if lock:
        await conn.execute(
            "SELECT 1 FROM booking.trip_plans WHERE source_system = 'cis' AND source_id = $1 FOR UPDATE", source_id
        )
    plan = await conn.fetchrow(PLAN_CALENDAR_SQL, source_id)
    if plan is None:
        raise NotFound("trip_plan_not_found", "this tour has no trip plan yet")
    return plan


async def plan_calendar(conn: asyncpg.Connection, source_id: str) -> dict[str, Any]:
    """The plan's calendar and the change waiting for a second person, if any (``request``)."""
    plan = await _plan_calendar(conn, source_id)
    out: dict[str, Any] = {
        "source_id": source_id,
        "season_calendar_id": plan["calendar_id"],
        "season_calendar_name": plan["calendar_name"],
        "request": None,
    }
    if plan["requested_by"] is not None:
        request = {
            "season_calendar_id": plan["pending_calendar_id"],
            "season_calendar_name": plan["pending_calendar_name"],
            "requested_by": plan["requested_by"],
            "requested_at": plan["requested_at"],
            "reason": plan["request_reason"],
        }
        out["request"] = (await _people(conn, [request], ("requested_by",)))[0]
    return out


async def request_plan_calendar(
    conn: asyncpg.Connection, staff: Staff, source_id: str, calendar_id: str | None, reason: str | None
) -> dict[str, Any]:
    """Ask for another sell-side calendar of a plan (a destination calendar), or none; a second person approves it
    (:func:`decide_plan_calendar`). 409 ``calendar_change_pending`` while one waits, ``no_change`` for the same."""
    _write(staff)
    text = _text(reason, "reason", REASON_MAX, required=True)
    plan = await _plan_calendar(conn, source_id, lock=True)
    if plan["requested_by"] is not None:
        raise Conflict("calendar_change_pending", "a change of this plan's calendar waits for approval")
    if calendar_id is not None:
        row = await _calendar(conn, calendar_id)
        if row["owner_kind"] != "destination":
            raise Invalid("invalid_calendar", "a trip plan uses the sell-side calendar of a destination")
    if calendar_id == plan["calendar_id"]:
        raise Conflict("no_change", "the plan already uses this calendar")
    await conn.execute(
        "UPDATE booking.trip_plans SET pending_season_calendar_id = $2::uuid, season_calendar_requested_by = $3,"
        " season_calendar_requested_at = now(), season_calendar_request_reason = $4 WHERE id = $1::uuid",
        plan["id"],
        calendar_id,
        staff.cis_user_id,
        text,
    )
    await _plan_audit(conn, staff, "trip_plan.season_calendar_requested", plan, calendar_id, text, source_id)
    return await plan_calendar(conn, source_id)


async def decide_plan_calendar(
    conn: asyncpg.Connection, staff: Staff, source_id: str, action: str, reason: str | None
) -> dict[str, Any]:
    """``approve`` / ``reject`` (a second person with season.approve; a reject needs a reason) or ``withdraw`` (the
    requester, with a reason) the waiting change of a plan's calendar. Approving applies it: the cost sheet's next
    run reprices the plan's matrix with it (the plan row's updated_at is in its watermark)."""
    if action not in ("approve", "reject", "withdraw"):
        raise Invalid("invalid_action", action)
    text = _reason_for(action, reason)
    plan = await _plan_calendar(conn, source_id, lock=True)
    if plan["requested_by"] is None:
        raise Conflict("nothing_pending", "no change of this plan's calendar waits")
    if action == "withdraw":
        _write(staff)
        if plan["requested_by"] != staff.cis_user_id:
            raise Conflict("not_withdrawable", "only who asked for the change withdraws it")
    else:
        _approve(staff, plan["requested_by"])
    applied = "season_calendar_id = pending_season_calendar_id, " if action == "approve" else ""
    await conn.execute(
        f"UPDATE booking.trip_plans SET {applied}pending_season_calendar_id = NULL,"  # noqa: S608 — constant parts
        " season_calendar_requested_by = NULL, season_calendar_requested_at = NULL,"
        " season_calendar_request_reason = NULL WHERE id = $1::uuid",
        plan["id"],
    )
    await _plan_audit(
        conn, staff, f"trip_plan.season_calendar_{action}", plan, plan["pending_calendar_id"], text, source_id
    )
    return await plan_calendar(conn, source_id)


async def _plan_audit(
    conn: asyncpg.Connection,
    staff: Staff,
    action: str,
    plan: Any,
    calendar_id: str | None,
    reason: str | None,
    source_id: str,
) -> None:
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=action,
        entity_type="trip_plan",
        entity_id=plan["id"],
        before={"season_calendar_id": plan["calendar_id"]},
        after={
            "season_calendar_id": calendar_id,
            "reason": reason,
            "requested_by": plan["requested_by"],
            "request_reason": plan["request_reason"],
        },
        entity_label=source_id[:80],
        summary=action.split(".")[1].replace("_", " ").capitalize(),
    )


UNASSIGNED_SQL = """
SELECT p.id::text AS id, t.tour_id
FROM booking.trip_plans p
JOIN LATERAL (SELECT t.tour_id FROM catalog.v_tour t
              WHERE t.source_system = p.source_system AND t.source_id = p.source_id LIMIT 1) t ON true
WHERE p.season_calendar_id IS NULL
ORDER BY md5(p.id::text || $2)
LIMIT $1
"""
# One tour at a time (a tour_id = $1 filter reaches the view's parse; an ANY() list does not, lessons "Dữ liệu").
DAY_ONE_SQL = """
SELECT pl.destination_id::text AS destination_id, pl.destination_name
FROM catalog.v_tour_place pl
WHERE pl.tour_id = $1 AND pl.day_number = 1 AND pl.destination_id IS NOT NULL
ORDER BY pl.position
LIMIT 1
"""
CALENDAR_FOR_SQL = """
WITH made AS (
    INSERT INTO booking.season_calendars (owner_kind, destination_id, name, created_by)
    VALUES ('destination', $1::uuid, $2, $3)
    ON CONFLICT (destination_id) WHERE destination_id IS NOT NULL DO NOTHING
    RETURNING id
)
SELECT id::text FROM made
UNION ALL
SELECT id::text FROM booking.season_calendars WHERE destination_id = $1::uuid
LIMIT 1
"""
SYSTEM = "system:bk_trip_plan_sync"


async def assign_default_calendars(conn: asyncpg.Connection, limit: int, salt: str) -> dict[str, int]:
    """Give up to ``limit`` plans without a calendar the calendar of the destination of their first day (made empty
    when missing). Plans whose first day names no destination stay without one (low season); ``salt`` rotates which
    plans are tried, so those never block the others."""
    out = {"assigned": 0, "calendars_created": 0, "no_destination": 0}
    for plan in await conn.fetch(UNASSIGNED_SQL, limit, salt):
        dest = await conn.fetchrow(DAY_ONE_SQL, plan["tour_id"])
        if dest is None:
            out["no_destination"] += 1
            continue
        before = await conn.fetchval(
            "SELECT count(*) FROM booking.season_calendars WHERE destination_id = $1::uuid", dest["destination_id"]
        )
        name = (dest["destination_name"] or "Destination")[:NAME_MAX]
        cal = await conn.fetchval(CALENDAR_FOR_SQL, dest["destination_id"], name, SYSTEM)
        out["calendars_created"] += int(not before)
        await conn.execute(
            "UPDATE booking.trip_plans SET season_calendar_id = $2::uuid WHERE id = $1::uuid AND"
            " season_calendar_id IS NULL",
            plan["id"],
            cal,
        )
        out["assigned"] += 1
    return out
