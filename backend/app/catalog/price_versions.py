"""Price list versions of a trip plan (AA-1091, bk-052; root ADR 0005b decisions 1, 2, 6, 9 and 10; delivery step 3).

Every hand price the team enters or changes is a **version**, never an overwrite (``PUT …/prices`` is refused since
bk-052). A version is the per-person selling price list of one plan for one travel window:

- **draft**: made by its maker (empty, or copied from the prices in force or from another version), edited only by
  them: label, tariff year, travel window, optional sale window (quote days it may be used on), rows (the price
  matrix rows; a row with a net cost is priced on save by the channel rule, FX and rounding, with the peak uplift
  when the plan's season calendar says peak or festive on the row's first day). Prices nothing.
- **pending**: the maker submits it with a reason (at most 300 characters). The **diff** (per row: the price in
  force for the same channel, currency and pax band on the row's first day, the new price, the change in percent,
  highlighted above the policy's ``diff_highlight_pct``, below the floor flagged) and the **impact** (open quotes and
  departures of the plan in the window, b2b rows that change) are computed and frozen with their SHA-256. At most one
  pending version per plan and overlapping window (409 ``pending_exists``).
- **published**: a second person approves it (``price_version.approve``; never the maker: 403 ``maker_checker``),
  sending the SHA-256 of the diff they saw (409 ``diff_changed`` on a mismatch). The superseded states of the plan's
  versions are recomputed (:mod:`app.catalog.version_matrix`) and the matrix is rebuilt at once (the cost sheet's
  ``recompute_plan`` under the checker's name: hand rows from the winning versions, computed rows around them).
- **rejected** by a second person with a reason; **withdrawn** by the maker (draft or pending) or, for a published
  version, by a second person with a reason: the overlap rule then runs on the versions left, so a version the
  withdrawn one had superseded prices its uncovered dates again; :func:`withdraw_preview` shows that outcome first.
  Both are final. A version is never deleted.

Dates: the API shows and takes inclusive end dates (``travel_to``, ``sale_to``, row ``valid_to``); the tables keep
half-open ranges (d10).

Not here (later steps of the ADR): the diff before approval of contracts, rules and FX and the cost sheet's
auto-publish rule (step 5: ``source = cost_sheet`` versions; today the cost sheet keeps writing its computed rows
around the published hand rows), quotes pinning ``price_version_id`` (step 6: the matrix row carries ``version_id``),
tariff versions (step 7), the versioned Excel export and import and copy to next year (step 8: ``source = import``
with ``source_sha256``, ``provisional``). The bulk workbook import (AA-1091 item 4) writes its Prices sheet as a
draft per tour (``source = import``), never live.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.catalog import cost_sheet, trip_plan_prices, version_matrix
from app.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from app.pricing import engine, policy_values, repository, seasons

ONE_DAY = timedelta(days=1)
REASON_MAX = 300
LABEL_MAX = 80
SOURCES = ("staff", "import", "cost_sheet", "provisional")
OPEN = ("draft", "pending")
SHOWN_REFS = 20

VERSION_SQL = """
SELECT v.id::text AS id, v.trip_plan_id::text AS trip_plan_id, p.source_id, v.version_no, v.label, v.tariff_year,
       v.travel_from, v.travel_to, v.sale_from, v.sale_to, v.status, v.source, v.reason,
       v.replaces_id::text AS replaces_id,
       v.created_by, v.created_at, v.updated_at, v.submitted_at, v.approved_by, v.approved_at, v.rejected_by,
       v.rejected_at, v.reject_reason, v.withdrawn_by, v.withdrawn_at, v.withdraw_reason, v.superseded_at,
       v.diff_sha256, (SELECT count(*) FROM booking.price_list_version_rows r WHERE r.version_id = v.id) AS rows
FROM booking.price_list_versions v JOIN booking.trip_plans p ON p.id = v.trip_plan_id
"""
ROWS_SQL = """
SELECT r.position, r.channel, r.season, r.travel_from, r.travel_to, r.pax_min, r.pax_max, r.currency::text AS currency,
       r.amount::text AS amount, r.rrp_amount::text AS rrp_amount, r.net_amount::text AS net_amount,
       r.net_currency::text AS net_currency, r.effective_margin::text AS effective_margin, r.priced_on, r.overridden,
       r.computed, r.child_amount::text AS child_amount, r.infant_amount::text AS infant_amount,
       r.single_supplement_amount::text AS single_supplement_amount, r.child_net_amount::text AS child_net_amount,
       r.infant_net_amount::text AS infant_net_amount,
       r.single_supplement_net_amount::text AS single_supplement_net_amount
FROM booking.price_list_version_rows r WHERE r.version_id = $1::uuid ORDER BY r.position
"""
INSERT_ROW_SQL = """
INSERT INTO booking.price_list_version_rows (version_id, position, channel, season, travel_from, travel_to, pax_min,
    pax_max, currency, amount, rrp_amount, net_amount, net_currency, effective_margin, priced_on, overridden,
    child_amount, infant_amount, single_supplement_amount, child_net_amount, infant_net_amount,
    single_supplement_net_amount)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22)
"""
PAX_FIELDS = trip_plan_prices.PAX_TYPE_FIELDS


def today() -> date:
    return datetime.now(UTC).date()


# ── Pure helpers ─────────────────────────────────────────────────────────────


def _md(start_md: str) -> tuple[int, int]:
    month, day = start_md.split("-")
    return int(month), int(day)


def _on(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:  # 02-29 in a common year
        return date(year, month, day - 1)


def tariff_year_of(day: date, start_md: str) -> int:
    """The tariff year a travel date belongs to, named by the calendar year it ends in (start 11-01: 2026-11-01 to
    2027-10-31 is tariff year 2027)."""
    month, dd = _md(start_md)
    start = _on(day.year, month, dd)
    if day < start:
        start = _on(day.year - 1, month, dd)
    return (_on(start.year + 1, month, dd) - ONE_DAY).year


def tariff_year_window(year: int, start_md: str) -> tuple[date, date]:
    """``[start, end)`` of a tariff year."""
    month, dd = _md(start_md)
    for start in (_on(year - 1, month, dd), _on(year, month, dd)):
        if tariff_year_of(start, start_md) == year:
            return start, _on(start.year + 1, month, dd)
    raise ValueError(year)  # pragma: no cover — one of the two always ends in ``year``


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _band(row: dict[str, Any]) -> tuple[str, str, int, int | None]:
    return (row["channel"], str(row["currency"]).strip(), int(row["pax_min"]), row["pax_max"])


def diff_rows(
    old: Sequence[dict[str, Any]],
    new: Sequence[dict[str, Any]],
    window: tuple[date, date],
    highlight_pct: Decimal,
    floors: dict[str, Decimal],
) -> dict[str, Any]:
    """The frozen diff of a version (d9). ``old`` = the plan's matrix in force (inclusive ``valid_to``); ``new`` = the
    version's rows (inclusive ``valid_to``); ``window`` = the version's half-open travel window. Per new row: the old
    price of the same channel, currency and pax band in force on its first day (None = added), the change in percent
    (rounded to 0.1), ``highlight`` above ``highlight_pct`` either way, ``below_floor`` when a row priced from a net
    cost sells under its channel's floor margin. ``removed``: rows in force in the window that no new row of the same
    band covers."""
    out_rows: list[dict[str, Any]] = []
    summary = {"added": 0, "changed": 0, "unchanged": 0, "removed": 0, "up": 0, "down": 0, "highlighted": 0}
    for row in new:
        same = [o for o in old if _band(o) == _band(row) and o["valid_from"] <= row["valid_from"] <= o["valid_to"]]
        before = _dec(same[0]["amount"]) if same else None
        after = _dec(row["amount"])
        assert after is not None  # noqa: S101 — amount is NOT NULL
        pct = None
        if before:
            pct = ((after - before) / before * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        kind = "added" if before is None else ("unchanged" if before == after else "changed")
        margin = _dec(row.get("effective_margin"))
        floor = floors.get(row["channel"])
        below = margin is not None and floor is not None and margin < floor
        highlight = pct is not None and abs(pct) > highlight_pct
        summary[kind] += 1
        summary["up"] += int(pct is not None and pct > 0)
        summary["down"] += int(pct is not None and pct < 0)
        summary["highlighted"] += int(highlight)
        out_rows.append(
            {
                "position": row["position"],
                "channel": row["channel"],
                "currency": str(row["currency"]).strip(),
                "pax_min": row["pax_min"],
                "pax_max": row["pax_max"],
                "season": row.get("season"),
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "old_amount": None if before is None else str(before),
                "new_amount": str(after),
                "change_pct": None if pct is None else str(pct),
                "kind": kind,
                "highlight": highlight,
                "below_floor": below,
            }
        )
    removed: list[dict[str, Any]] = []
    last = window[1] - ONE_DAY
    for o in old:
        if o["valid_to"] < window[0] or o["valid_from"] > last:
            continue
        covered = any(
            _band(n) == _band(o) and n["valid_from"] <= o["valid_to"] and o["valid_from"] <= n["valid_to"] for n in new
        )
        if not covered:
            removed.append(
                {
                    "channel": o["channel"],
                    "currency": str(o["currency"]).strip(),
                    "pax_min": o["pax_min"],
                    "pax_max": o["pax_max"],
                    "valid_from": max(o["valid_from"], window[0]),
                    "valid_to": min(o["valid_to"], last),
                    "old_amount": str(_dec(o["amount"])),
                }
            )
    summary["removed"] = len(removed)
    return {"rows": out_rows, "removed": removed, "summary": summary, "highlight_pct": str(highlight_pct)}


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _inclusive(end: date | None) -> date | None:
    return None if end is None else end - ONE_DAY


def version_out(row: Any) -> dict[str, Any]:
    """The API shape of a version (inclusive end dates)."""
    out = {k: row[k] for k in row.keys() if k not in ("rows",)}
    out["travel_to"] = _inclusive(row["travel_to"])
    out["sale_to"] = _inclusive(row["sale_to"])
    out["row_count"] = int(row["rows"])
    return out


def row_out(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["valid_from"] = out.pop("travel_from")
    out["valid_to"] = out.pop("travel_to") - ONE_DAY
    out["currency"] = out["currency"].strip()
    out["net_currency"] = (out["net_currency"] or "").strip() or None
    return out


# ── Reads ────────────────────────────────────────────────────────────────────


def _can_read(staff: Staff) -> None:
    if not staff.can("pricing.read"):
        raise Forbidden("forbidden", "prices are commercial data (pricing.read)")


PLAN_SQL = (
    "SELECT id::text AS id, source_id, season_calendar_id::text AS calendar_id FROM booking.trip_plans"
    " WHERE source_system = 'cis' AND source_id = $1"
)


async def _plan(conn: asyncpg.Connection, source_id: str, *, lock: bool = False) -> asyncpg.Record:
    sql = PLAN_SQL + (" FOR UPDATE" if lock else "")
    row = await conn.fetchrow(sql, source_id)
    if row is None:
        raise NotFound("trip_plan_not_found", "save the commercial details of this tour first")
    return row


async def _version(conn: asyncpg.Connection, version_id: str, *, lock: bool = False) -> asyncpg.Record:
    try:
        row = await conn.fetchrow(VERSION_SQL + " WHERE v.id = $1::uuid", version_id)
    except asyncpg.DataError:
        row = None
    if row is None:
        raise NotFound("price_version_not_found", "no such price version")
    if lock:
        # Every write locks the plan first: one writer per plan (version numbers, pending rule, the matrix).
        await conn.execute("SELECT 1 FROM booking.trip_plans WHERE id = $1::uuid FOR UPDATE", row["trip_plan_id"])
        row = await conn.fetchrow(VERSION_SQL + " WHERE v.id = $1::uuid", version_id)
    assert row is not None  # noqa: S101 — read just above
    return row


async def in_force(conn: asyncpg.Connection, plan_id: str, day: date) -> list[dict[str, Any]]:
    """Which version prices which dates for a quote made on ``day`` (inclusive end dates)."""
    versions = await version_matrix.published(conn, plan_id)
    by_id = {v.id: v for v in versions}
    out = []
    for vid, spans in version_matrix.winning(versions, day).items():
        for start, end in spans:
            out.append({"version_id": vid, "version_no": by_id[vid].version_no, "from": start, "to": end - ONE_DAY})
    return sorted(out, key=lambda s: s["from"])


async def list_versions(conn: asyncpg.Connection, staff: Staff, source_id: str) -> dict[str, Any]:
    """The plan's versions (newest first), which version prices which dates today, and its season calendar."""
    _can_read(staff)
    plan = await _plan(conn, source_id)
    rows = await conn.fetch(VERSION_SQL + " WHERE v.trip_plan_id = $1::uuid ORDER BY v.version_no DESC", plan["id"])
    cal = await seasons.for_plan(conn, plan["id"])
    return {
        "source_id": source_id,
        "items": [version_out(r) for r in rows],
        "in_force": await in_force(conn, plan["id"], today()),
        "calendar": {"id": cal.id, "name": cal.name, "default_season": cal.default} if cal.id else None,
    }


async def get_version(conn: asyncpg.Connection, staff: Staff, version_id: str) -> dict[str, Any]:
    """One version with its rows, frozen diff and impact."""
    _can_read(staff)
    row = await _version(conn, version_id)
    extra = await conn.fetchrow("SELECT diff, impact FROM booking.price_list_versions WHERE id = $1::uuid", version_id)
    return {
        **version_out(row),
        "items": [row_out(r) for r in await conn.fetch(ROWS_SQL, version_id)],
        "diff": _json(extra["diff"]),
        "impact": _json(extra["impact"]),
    }


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def pending(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    """Every pending version (the checker's queue), oldest first."""
    _can_read(staff)
    rows = await conn.fetch(VERSION_SQL + " WHERE v.status = 'pending' ORDER BY v.submitted_at")
    return {"items": [version_out(r) for r in rows]}


# ── Draft ────────────────────────────────────────────────────────────────────


def _can_write(staff: Staff) -> None:
    if not staff.can("price_version.write"):
        raise Forbidden("forbidden", "making a price version needs price_version.write")


def _text(value: str | None, field: str, limit: int, *, required: bool = False) -> str | None:
    text = (value or "").strip() or None
    if text is None and required:
        raise Invalid(f"{field}_required", f"give a {field.replace('_', ' ')}")
    if text is not None and len(text) > limit:
        raise Invalid(f"{field}_too_long", f"the {field.replace('_', ' ')} is at most {limit} characters")
    return text


def _window(body: dict[str, Any], start_md: str) -> dict[str, Any]:
    """The facts of a draft from the request (inclusive ends in, half-open out)."""
    travel_from: date = body["travel_from"]
    travel_last: date = body["travel_to"]
    if travel_last < travel_from:
        raise Invalid("invalid_window", "the last travel date is before the first")
    sale_from, sale_last = body.get("sale_from"), body.get("sale_to")
    if sale_from and sale_last and sale_last < sale_from:
        raise Invalid("invalid_sale_window", "the last sale date is before the first")
    year = body.get("tariff_year") or tariff_year_of(travel_from, start_md)
    label = _text(body.get("label"), "label", LABEL_MAX) or f"{year} prices"
    return {
        "label": label,
        "tariff_year": int(year),
        "travel_from": travel_from,
        "travel_to": travel_last + ONE_DAY,
        "sale_from": sale_from,
        "sale_to": None if sale_last is None else sale_last + ONE_DAY,
        "reason": _text(body.get("reason"), "reason", REASON_MAX),
    }


async def _policy(conn: asyncpg.Connection, day: date) -> policy_values.Policy:
    policy = await policy_values.load(conn, day)
    try:
        policy.tariff_year_start  # noqa: B018 — fail here with a clear 503
        policy.diff_highlight_pct  # noqa: B018
    except policy_values.PolicyUnavailable as exc:
        raise Unavailable("policy_unavailable", str(exc)) from None
    return policy


def _clip(row: dict[str, Any], window: tuple[date, date]) -> dict[str, Any] | None:
    """A matrix row (inclusive) cut to a half-open window, None when outside it."""
    start, last = max(row["valid_from"], window[0]), min(row["valid_to"], window[1] - ONE_DAY)
    return None if last < start else {**row, "valid_from": start, "valid_to": last}


def _as_input(row: dict[str, Any]) -> dict[str, Any]:
    """A stored row as a draft input row (amounts as stored: a copy never re-prices)."""
    out = {
        k: row.get(k)
        for k in ("channel", "season", "valid_from", "valid_to", "pax_min", "pax_max", "currency", "amount")
    }
    for k in PAX_FIELDS:
        if k in trip_plan_prices.PAX_TYPE_AMOUNTS:
            out[k] = _dec(row.get(k))
    out["amount"] = _dec(row["amount"])
    out["currency"] = str(row["currency"]).strip()
    return out


async def create_draft(conn: asyncpg.Connection, staff: Staff, source_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """A new draft of the plan. ``copy_from``: ``in_force`` (the hand prices of the matrix in force, cut to the
    window; default), ``none`` or a version id of the same plan (its hand rows, cut to the window). The window
    defaults to the tariff year of today (policy ``tariff_year_start``)."""
    _can_write(staff)
    plan = await _plan(conn, source_id, lock=True)
    day = today()
    policy = await _policy(conn, day)
    if not body.get("travel_from") or not body.get("travel_to"):
        start, end = tariff_year_window(tariff_year_of(day, policy.tariff_year_start), policy.tariff_year_start)
        body = {
            **body,
            "travel_from": body.get("travel_from") or start,
            "travel_to": body.get("travel_to") or end - ONE_DAY,
        }
    facts = _window(body, policy.tariff_year_start)
    window = (facts["travel_from"], facts["travel_to"])
    source = body.get("copy_from") or "in_force"
    replaces: str | None = None
    if source == "in_force":
        rows = [dict(r) for r in await conn.fetch(trip_plan_prices.ROWS_SQL, plan["id"])]
        copied = [r for r in rows if r.get("version_id")]
        latest = await conn.fetchval(
            "SELECT id::text FROM booking.price_list_versions WHERE trip_plan_id = $1::uuid AND status = 'published'"
            " ORDER BY approved_at DESC, version_no DESC LIMIT 1",
            plan["id"],
        )
        replaces = latest
    elif source == "none":
        copied = []
    else:
        other = await _version(conn, str(source))
        if other["trip_plan_id"] != plan["id"]:
            raise Invalid("invalid_copy_from", "copy from a version of the same tour")
        copied = [row_out(r) for r in await conn.fetch(ROWS_SQL, other["id"]) if not r["computed"] or r["overridden"]]
        replaces = other["id"]
    clipped = [c for c in (_clip(r, window) for r in copied) if c is not None]
    vid = await conn.fetchval(
        """
        INSERT INTO booking.price_list_versions (trip_plan_id, version_no, label, tariff_year, travel_from, travel_to,
            sale_from, sale_to, reason, replaces_id, source, created_by)
        VALUES ($1::uuid, (SELECT coalesce(max(version_no), 0) + 1 FROM booking.price_list_versions
                           WHERE trip_plan_id = $1::uuid), $2, $3, $4, $5, $6, $7, $8, $9::uuid, 'staff', $10)
        RETURNING id::text
        """,
        plan["id"],
        facts["label"],
        facts["tariff_year"],
        facts["travel_from"],
        facts["travel_to"],
        facts["sale_from"],
        facts["sale_to"],
        facts["reason"],
        replaces,
        staff.cis_user_id,
    )
    if clipped:
        await _write_rows(conn, vid, plan["id"], [_as_input(r) for r in _merge_pieces(clipped)], None, day)
    await _audit(conn, staff, "price_version.created", vid, source_id, {"copy_from": source, **facts})
    return await get_version(conn, staff, vid)


def _merge_pieces(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join the matrix pieces of one version row (cut where the season changes) back into one row."""
    out: list[dict[str, Any]] = []
    key_fields = ("channel", "season", "pax_min", "pax_max", "currency", "amount", *trip_plan_prices.PAX_TYPE_AMOUNTS)
    for row in sorted(rows, key=lambda r: (r["channel"], str(r["currency"]), r["pax_min"], r["valid_from"])):
        if out:
            prev = out[-1]
            same = all(str(prev.get(k)) == str(row.get(k)) for k in key_fields)
            if same and prev["valid_to"] + ONE_DAY == row["valid_from"]:
                out[-1] = {**prev, "valid_to": row["valid_to"]}
                continue
        out.append(dict(row))
    return out


async def _write_rows(
    conn: asyncpg.Connection,
    version_id: str,
    plan_id: str,
    items: list[dict[str, Any]],
    below_floor_reason: str | None,
    day: date,
) -> list[dict[str, Any]]:
    """Price and store a draft's rows (replacing them): the calendar's peak on each row's first day, the same
    validation and pricing as the matrix form (:func:`app.catalog.trip_plan_prices.price_rows`)."""
    cal = await seasons.for_plan(conn, plan_id)
    rows = [{**r, "computed": False, "peak": cal.season_on(r["valid_from"]).peak} for r in items]
    final = await trip_plan_prices.price_rows(conn, rows, below_floor_reason=below_floor_reason, on=day)
    await conn.execute("DELETE FROM booking.price_list_version_rows WHERE version_id = $1::uuid", version_id)
    await conn.executemany(
        INSERT_ROW_SQL,
        [
            (
                version_id,
                r["position"],
                r["channel"],
                r["season"],
                r["valid_from"],
                r["valid_to"] + ONE_DAY,
                r["pax_min"],
                r["pax_max"],
                r["currency"],
                r["amount"],
                r["rrp_amount"],
                r["net_amount"],
                r["net_currency"],
                r["effective_margin"],
                r["priced_on"],
                r["overridden"],
                *(_dec(r.get(k)) for k in PAX_FIELDS),
            )
            for r in final
        ],
    )
    return final


def _own_draft(row: Any, staff: Staff) -> None:
    if row["status"] != "draft":
        raise Conflict("not_a_draft", f"version {row['version_no']} is {row['status']}: make a new version")
    if row["created_by"] != staff.cis_user_id:
        raise Forbidden("not_the_maker", "only the person who made a draft edits or submits it")


async def save_draft(conn: asyncpg.Connection, staff: Staff, version_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """The maker replaces the facts and rows of their draft. Every row lies inside the travel window."""
    _can_write(staff)
    row = await _version(conn, version_id, lock=True)
    _own_draft(row, staff)
    day = today()
    policy = await _policy(conn, day)
    facts = _window(body, policy.tariff_year_start)
    items = list(body.get("items") or [])
    for n, item in enumerate(items, start=1):
        if item["valid_from"] < facts["travel_from"] or item["valid_to"] >= facts["travel_to"]:
            raise Invalid("row_outside_window", f"row {n}: its dates must lie inside the version's travel dates", row=n)
    final = await _write_rows(conn, version_id, row["trip_plan_id"], items, body.get("below_floor_reason"), day)
    await conn.execute(
        "UPDATE booking.price_list_versions SET label = $2, tariff_year = $3, travel_from = $4, travel_to = $5,"
        " sale_from = $6, sale_to = $7, reason = $8, updated_at = now() WHERE id = $1::uuid",
        version_id,
        facts["label"],
        facts["tariff_year"],
        facts["travel_from"],
        facts["travel_to"],
        facts["sale_from"],
        facts["sale_to"],
        facts["reason"],
    )
    below = _text(body.get("below_floor_reason"), "below_floor_reason", REASON_MAX)
    await _audit(
        conn, staff, "price_version.saved", version_id, row["source_id"],
        {"rows": len(final), "below_floor_reason": below, **facts},
    )  # fmt: skip
    return await get_version(conn, staff, version_id)


# ── Submit, approve, reject, withdraw ────────────────────────────────────────


async def _floors(conn: asyncpg.Connection, day: date) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for channel in engine.Channel:
        try:
            dated = await repository.rule_in_force(conn, channel, day)
        except Exception:  # noqa: BLE001, S112 — a stored rule the engine refuses: no floor flag for that channel
            continue
        if dated is not None:
            out[channel.value] = dated.rule.floor_margin
    return out


IMPACT_QUOTES_SQL = """
SELECT DISTINCT q.id::text AS id
FROM booking.b2b_quotes q JOIN booking.b2b_quote_lines l ON l.quote_id = q.id
WHERE l.component_id = $1 AND q.valid_until > now() AND l.service_date >= $2 AND l.service_date < $3
"""
IMPACT_DEPARTURES_SQL = """
SELECT d.start_date FROM booking.departures d
WHERE d.trip_plan_id = $1::uuid AND d.status <> 'cancelled' AND d.start_date >= $2 AND d.start_date < $3
ORDER BY d.start_date
"""


async def impact(conn: asyncpg.Connection, plan_id: str, window: tuple[date, date], diff: dict[str, Any]) -> dict:
    """What the version touches (d9): open quotes with a line of the plan in the window (they re-price when they
    expire), the plan's departures in the window and the b2b rows (tariff, RRP) that change."""
    quotes = [r["id"] for r in await conn.fetch(IMPACT_QUOTES_SQL, f"tp:{plan_id}", *window)]
    departures = [r["start_date"] for r in await conn.fetch(IMPACT_DEPARTURES_SQL, plan_id, *window)]
    b2b = [r for r in diff["rows"] if r["channel"] == engine.Channel.B2B.value and r["kind"] != "unchanged"]
    return {
        "open_quotes": len(quotes),
        "open_quote_ids": quotes[:SHOWN_REFS],
        "departures": len(departures),
        "departure_dates": departures[:SHOWN_REFS],
        "b2b_rows_changed": len(b2b),
        "rows_removed": len(diff["removed"]),
    }


async def submit(conn: asyncpg.Connection, staff: Staff, version_id: str, reason: str | None) -> dict[str, Any]:
    """The maker submits their draft with a reason; the diff and impact are frozen (d9)."""
    _can_write(staff)
    row = await _version(conn, version_id, lock=True)
    _own_draft(row, staff)
    text = _text(reason, "reason", REASON_MAX) or row["reason"]
    if not text:
        raise Invalid("reason_required", "give the reason for this price change (at most 300 characters)")
    if not row["rows"]:
        raise Invalid("no_rows", "a price version needs at least one price row")
    clash = await conn.fetchval(
        "SELECT version_no FROM booking.price_list_versions WHERE trip_plan_id = $1::uuid AND status = 'pending'"
        " AND travel_from < $3 AND $2 < travel_to AND id <> $4::uuid",
        row["trip_plan_id"],
        row["travel_from"],
        row["travel_to"],
        version_id,
    )
    if clash is not None:
        raise Conflict(
            "pending_exists",
            f"version {clash} of this tour covers some of these dates and waits for approval: decide it first",
            version_no=clash,
        )
    day = today()
    policy = await _policy(conn, day)
    old = [dict(r) for r in await conn.fetch(trip_plan_prices.ROWS_SQL, row["trip_plan_id"])]
    new = [row_out(r) for r in await conn.fetch(ROWS_SQL, version_id)]
    window = (row["travel_from"], row["travel_to"])
    diff = json.loads(canonical(diff_rows(old, new, window, policy.diff_highlight_pct, await _floors(conn, day))))
    effect = json.loads(canonical(await impact(conn, row["trip_plan_id"], window, diff)))
    digest = sha256(diff)
    await conn.execute(
        "UPDATE booking.price_list_versions SET status = 'pending', reason = $2, submitted_at = now(),"
        " diff = $3::jsonb, impact = $4::jsonb, diff_sha256 = $5, updated_at = now() WHERE id = $1::uuid",
        version_id,
        text,
        canonical(diff),
        canonical(effect),
        digest,
    )
    await _audit(
        conn, staff, "price_version.submitted", version_id, row["source_id"], {"reason": text, "diff_sha256": digest}
    )
    return await get_version(conn, staff, version_id)


def _checker(row: Any, staff: Staff) -> None:
    if not staff.can("price_version.approve"):
        raise Forbidden("forbidden", "approving prices needs price_version.approve")
    if row["created_by"] == staff.cis_user_id:
        raise Forbidden("maker_checker", "a second person approves, rejects or withdraws a price version")


async def refresh_states(conn: asyncpg.Connection, plan_id: str) -> dict[str, list[str]]:
    """Recompute published / superseded of the plan's versions from the published set (d2)."""
    versions = await version_matrix.published(conn, plan_id)
    gone = version_matrix.superseded(versions)
    states = {r["id"]: r["status"] for r in await conn.fetch(version_matrix.PUBLISHED_SQL, plan_id)}
    moved: dict[str, list[str]] = {"superseded": [], "published": []}
    for vid, status in states.items():
        want = "superseded" if vid in gone else "published"
        if want != status:
            await conn.execute(
                "UPDATE booking.price_list_versions SET status = $2,"
                " superseded_at = CASE WHEN $2 = 'superseded' THEN now() END, updated_at = now() WHERE id = $1::uuid",
                vid,
                want,
            )
            moved[want].append(vid)
    return moved


async def _rebuild(conn: asyncpg.Connection, staff: Staff, plan_id: str, action: str, version_id: str) -> dict:
    moved = await refresh_states(conn, plan_id)
    pricing = await cost_sheet.load_pricing(conn, today())
    result = await cost_sheet.recompute_plan(
        conn,
        plan_id,
        pricing,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=action,
        extra={"price_version_id": version_id, "states_moved": moved},
    )
    return {"states_moved": moved, "matrix": result.get("outcome")}


async def approve(conn: asyncpg.Connection, staff: Staff, version_id: str, diff_sha256: str) -> dict[str, Any]:
    """A second person publishes a pending version, having seen its diff (the SHA-256 must match)."""
    row = await _version(conn, version_id, lock=True)
    _checker(row, staff)
    if row["status"] != "pending":
        raise Conflict("not_pending", f"version {row['version_no']} is {row['status']}")
    if (diff_sha256 or "").strip().lower() != row["diff_sha256"]:
        raise Conflict("diff_changed", "the diff you saw is not the one submitted: reload the version")
    await conn.execute(
        "UPDATE booking.price_list_versions SET status = 'published', approved_by = $2, approved_at = now(),"
        " updated_at = now() WHERE id = $1::uuid",
        version_id,
        staff.cis_user_id,
    )
    effect = await _rebuild(conn, staff, row["trip_plan_id"], "price_version.published", version_id)
    await _audit(conn, staff, "price_version.approved", version_id, row["source_id"], effect)
    return {**(await get_version(conn, staff, version_id)), "effect": effect}


async def reject(conn: asyncpg.Connection, staff: Staff, version_id: str, reason: str | None) -> dict[str, Any]:
    row = await _version(conn, version_id, lock=True)
    _checker(row, staff)
    if row["status"] != "pending":
        raise Conflict("not_pending", f"version {row['version_no']} is {row['status']}")
    text = _text(reason, "reason", REASON_MAX, required=True)
    await conn.execute(
        "UPDATE booking.price_list_versions SET status = 'rejected', rejected_by = $2, rejected_at = now(),"
        " reject_reason = $3, updated_at = now() WHERE id = $1::uuid",
        version_id,
        staff.cis_user_id,
        text,
    )
    await _audit(conn, staff, "price_version.rejected", version_id, row["source_id"], {"reason": text})
    return await get_version(conn, staff, version_id)


def _outcome(
    versions: Sequence[version_matrix.Version], without: str, window: tuple[date, date], day: date
) -> dict[str, Any]:
    """Which version prices each date of ``window`` once ``without`` leaves the published set, and the dates no
    version prices any more (not quotable)."""
    left = [v for v in versions if v.id != without]
    by_id = {v.id: v for v in left}
    wins = version_matrix.winning(left, day)
    priced = []
    for vid, spans in wins.items():
        for span in spans:
            part = version_matrix.intersect(span, window)
            if part:
                priced.append({"version_id": vid, "version_no": by_id[vid].version_no, "from": part[0],
                               "to": part[1] - ONE_DAY})  # fmt: skip
    gaps = [{"from": a, "to": b - ONE_DAY} for a, b in version_matrix.uncovered(window, left, day)]
    return {"priced_by": sorted(priced, key=lambda p: p["from"]), "unquotable": gaps}


async def withdraw_preview(conn: asyncpg.Connection, staff: Staff, version_id: str) -> dict[str, Any]:
    """What a withdrawal of a published version would do (shown in the withdraw dialog before it is confirmed)."""
    _can_read(staff)
    row = await _version(conn, version_id)
    versions = await version_matrix.published(conn, row["trip_plan_id"])
    return _outcome(versions, version_id, (row["travel_from"], row["travel_to"]), today())


async def withdraw(conn: asyncpg.Connection, staff: Staff, version_id: str, reason: str | None) -> dict[str, Any]:
    """The maker withdraws their draft or pending version; a second person withdraws a published one (d2)."""
    row = await _version(conn, version_id, lock=True)
    text = _text(reason, "reason", REASON_MAX, required=True)
    if row["status"] in OPEN:
        _can_write(staff)
        if row["created_by"] != staff.cis_user_id:
            raise Forbidden("not_the_maker", "only its maker withdraws a draft or pending version")
    elif row["status"] == "published":
        _checker(row, staff)
    else:
        raise Conflict("not_withdrawable", f"version {row['version_no']} is {row['status']}")
    preview = None
    if row["status"] == "published":
        versions = await version_matrix.published(conn, row["trip_plan_id"])
        preview = _outcome(versions, version_id, (row["travel_from"], row["travel_to"]), today())
    await conn.execute(
        "UPDATE booking.price_list_versions SET status = 'withdrawn', withdrawn_by = $2, withdrawn_at = now(),"
        " withdraw_reason = $3, updated_at = now() WHERE id = $1::uuid",
        version_id,
        staff.cis_user_id,
        text,
    )
    effect: dict[str, Any] = {"reason": text}
    if preview is not None:
        effect.update(await _rebuild(conn, staff, row["trip_plan_id"], "price_version.withdrawn", version_id))
        effect["outcome"] = json.loads(canonical(preview))
    await _audit(conn, staff, "price_version.withdrawn", version_id, row["source_id"], effect)
    return {**(await get_version(conn, staff, version_id)), "effect": effect}


async def _audit(
    conn: asyncpg.Connection, staff: Staff, action: str, version_id: str, source_id: str, after: dict[str, Any]
) -> None:
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=action,
        entity_type="price_list_version",
        entity_id=version_id,
        after=json.loads(canonical({"source_id": source_id, **after})),
        entity_label=source_id[:80],
        summary=action.replace("price_version.", "Price version ").replace("_", " "),
    )


# ── Drafts from the bulk workbook (AA-1091 item 4) ───────────────────────────


def _row_key(row: dict[str, Any]) -> tuple[Any, ...]:
    def dec(v: Any) -> Decimal | None:
        return None if v is None else Decimal(str(v)).normalize()

    return (
        row["channel"],
        row.get("season") or None,
        row["valid_from"],
        row["valid_to"],
        int(row["pax_min"]),
        row.get("pax_max"),
        str(row["currency"]).strip(),
        dec(row["amount"]),
        *(dec(row.get(k)) for k in trip_plan_prices.PAX_TYPE_AMOUNTS),
    )


OPEN_IMPORT_SQL = """
SELECT id::text AS id, source_sha256 FROM booking.price_list_versions
WHERE trip_plan_id = $1::uuid AND status = 'draft' AND source = 'import' AND created_by = $2
ORDER BY version_no DESC
"""


async def import_draft(
    conn: asyncpg.Connection,
    staff: Staff,
    plan_id: str,
    source_id: str,
    items: list[dict[str, Any]],
    file_sha256: str,
) -> dict[str, Any]:
    """The Prices sheet of one tour as a draft (``source = import``, the file's SHA-256); nothing prices until its
    maker submits it from the tour's Versions tab and a second person approves it. Idempotent: rows equal to the hand
    prices in force, or a file already imported as the maker's open draft, write nothing (``unchanged``); a different
    file withdraws the maker's older open import draft of the tour and makes a new one."""
    current = _merge_pieces([dict(r) for r in await conn.fetch(trip_plan_prices.ROWS_SQL, plan_id) if r["version_id"]])
    plain = [r for r in current if r["net_amount"] is None]
    if sorted(map(_row_key, items), key=repr) == sorted(map(_row_key, plain), key=repr):
        return {"version_id": None, "rows": len(items), "unchanged": True}
    # A row priced from a net cost is not in the workbook: it stays in the new version unless a file row covers it
    # (the cost sheet's rule), its amount kept as typed.
    kept = [
        {**_as_input(r), "net_amount": _dec(r["net_amount"]), "net_currency": r["net_currency"]}
        for r in current
        if r["net_amount"] is not None and not any(cost_sheet.overlaps(r, i) for i in items)
    ]
    items = [*items, *kept]
    for old in await conn.fetch(OPEN_IMPORT_SQL, plan_id, staff.cis_user_id):
        if old["source_sha256"] == file_sha256:
            return {"version_id": old["id"], "rows": len(items), "unchanged": True}
        await conn.execute(
            "UPDATE booking.price_list_versions SET status = 'withdrawn', withdrawn_by = $2, withdrawn_at = now(),"
            " withdraw_reason = 'Replaced by a newer workbook import', updated_at = now() WHERE id = $1::uuid",
            old["id"],
            staff.cis_user_id,
        )
    day = today()
    policy = await _policy(conn, day)
    first = min(i["valid_from"] for i in items)
    last = max(i["valid_to"] for i in items)
    facts = _window({"travel_from": first, "travel_to": last}, policy.tariff_year_start)
    latest = await conn.fetchval(
        "SELECT id::text FROM booking.price_list_versions WHERE trip_plan_id = $1::uuid AND status = 'published'"
        " ORDER BY approved_at DESC, version_no DESC LIMIT 1",
        plan_id,
    )
    vid = await conn.fetchval(
        """
        INSERT INTO booking.price_list_versions (trip_plan_id, version_no, label, tariff_year, travel_from, travel_to,
            replaces_id, source, source_sha256, created_by)
        VALUES ($1::uuid, (SELECT coalesce(max(version_no), 0) + 1 FROM booking.price_list_versions
                           WHERE trip_plan_id = $1::uuid), $2, $3, $4, $5, $6::uuid, 'import', $7, $8)
        RETURNING id::text
        """,
        plan_id,
        f"{facts['tariff_year']} workbook import",
        facts["tariff_year"],
        facts["travel_from"],
        facts["travel_to"],
        latest,
        file_sha256,
        staff.cis_user_id,
    )
    await _write_rows(conn, vid, plan_id, items, None, day)
    await _audit(conn, staff, "price_version.imported", vid, source_id, {"rows": len(items), "sha256": file_sha256})
    return {"version_id": vid, "rows": len(items), "unchanged": False}
