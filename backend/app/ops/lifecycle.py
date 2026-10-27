"""The trip case lifecycle after the sale and the operations reminders (bk-027; AA-682 slice S2, S252 design §4, §9).

Two ``booking.job`` kinds, queued once per UTC day by :class:`OpsDailySchedule` (a worker maintenance hook, first
pass at or after :data:`SCHEDULE_UTC_HOUR` = 01:00 UTC = 08:00 in Vietnam; idempotency key ``<kind>:<UTC date>``).
Both are idempotent, run in seconds and are not exempt from the deploy gate. Payloads carry ids only (DM-6.3); results
carry case codes and counts only (no personal data).

**bk_case_lifecycle** moves sold cases along the travel dates (design §4), through the bk-026 edges and fact guard:

- ``pre_departure`` once ``start_date - pre_departure_days`` is reached, when the supply is ``booked`` or ``none`` and,
  for a B2C case, the case is ``paid`` (the money track, bk-028; until then every B2C case is blocked ``not_paid``). A
  B2B case moves from ``confirmed`` / ``supplier_confirming`` (never a money status);
- ``on_trip`` from ``pre_departure`` on the start date; ``completed`` from ``on_trip`` the day after the end date.

Moves chain in one run (a case the job missed for days catches up: confirmed → pre_departure → on_trip → completed),
each one a system UPDATE (empty ``booking.actor``, status note "Auto: …", audit actor ``aa682:case_lifecycle``) that
the database guard checks again. A gate that is due but blocked (``not_booked``, ``not_paid``, ``no_start_date``,
``no_end_date``, or ``refused`` by the guard) raises one staff notification ``trip_case.gate_blocked`` per case, target
and reasons (``notifications_reminder_uq``); the case keeps its status. Open trip checks never block a move (the trip
date is a fact); their count travels in the notice and the result. **Dates are read in the ops time zone**
(``b2b_ops_timezone``, default Asia/Ho_Chi_Minh): "today" is that zone's date and every write transaction sets the
session ``TimeZone`` to it, so the guard's ``current_date`` is the same day whatever the server or pool zone.

It also clears, once the trip ended ``emergency_contact_keep_days`` (90) ago, the emergency contact and the notes of the
case's trip checks (data standards DM-7 row of ``trip_case_checks``) and stamps ``cleared_at``.

Modes (DM-7.2 style): a manual run is a **DRY RUN** unless its payload says ``"dry_run": false``; a scheduled run is a
DRY RUN until one live run over every case (no ``case_ids``) has succeeded. A DRY RUN writes nothing but its job result:
the moves it would make, the blocked gates and the rows it would clear. ``case_ids`` (at most :data:`MAX_CASE_IDS`)
limits a manual run to those cases (the Dev live check on a synthetic case). Batches of :data:`BATCH` cases, each in
its own transaction, rows locked ``FOR UPDATE SKIP LOCKED`` (a case a staff request holds right now waits for the next
run); each case in a savepoint, so one refusal never undoes another case's move.

**bk_ops_reminders** writes staff notifications only (no e-mail), each once (``reminder_key``):

- ``supplier_booking.waiting``: a supplier booking ``requested`` more than ``supplier_answer_days`` ago, to the person
  who requested it (or the shared queue); key ``…:<booking>:<requested at>`` so a re-request reminds again;
- ``trip_check.due``: an open check of an open sold case due within ``trip_check_reminder_days`` (or overdue), to its
  assignee (or the shared queue); key ``…:<check>:<due date>`` so a new due date reminds again.

- ``payment.due_soon`` (S3, bk-028): a schedule row of a sent invoice due within ``payment_due_soon_days`` and not
  covered by verified payments, to the case's sales owner (or the shared queue) **and** a customer feed row (My Trips;
  no e-mail, design D15); key ``…:<row>:<due date>``;
- ``invoice.overdue`` (S3): such a row past its due date, at the steps of ``payment_overdue_reminder_days`` ([1, 7]),
  the highest step reached only, staff + customer rows; key ``…:<row>:<due date>:d<step>``.

The schedule state is read in the ops time zone (the transaction's ``TimeZone``). Supplier bill reminders (S4) join this
kind later. No job here ever writes money (design §9): reminders only.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg

from app import audit
from app.jobs import queue
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.notifications import service as notifications

log = logging.getLogger(__name__)

KIND = "bk_case_lifecycle"
REMINDERS_KIND = "bk_ops_reminders"
KINDS = (KIND, REMINDERS_KIND)
MAX_ATTEMPTS = 3
SCHEDULE_UTC_HOUR = 1
SCHEDULER = "scheduler:aa682-ops"
ACTOR = "aa682:case_lifecycle"
BATCH = 200
MAX_BATCHES = 25
MAX_CASE_IDS = 50
MAX_LISTED = 50
GATE_TYPE = "trip_case.gate_blocked"
WAITING_TYPE = "supplier_booking.waiting"
CHECK_DUE_TYPE = "trip_check.due"
DUE_SOON_TYPE = "payment.due_soon"
OVERDUE_TYPE = "invoice.overdue"
DEFAULT_OVERDUE_STEPS = (1, 7)
DEFAULT_TZ = "Asia/Ho_Chi_Minh"
TZ_SETTING = "b2b_ops_timezone"
DEFAULTS = {
    "pre_departure_days": 14,
    "emergency_contact_keep_days": 90,
    "supplier_answer_days": 2,
    "trip_check_reminder_days": 3,
}
LIMITS = {
    "pre_departure_days": (0, 120),
    "emergency_contact_keep_days": (0, 3650),
    "supplier_answer_days": (1, 60),
    "trip_check_reminder_days": (0, 60),
}
# Statuses a case leaves for pre_departure (bk-026 edges: B2B from confirmed / supplier_confirming, B2C from paid
# once bk-028 adds that edge); the money statuses are listed so a B2C case waiting for its money shows as blocked.
PRE_SOURCES = ("confirmed", "supplier_confirming", "invoice_sent", "payment_pending", "paid")
B2B_PRE_SOURCES = frozenset({"confirmed", "supplier_confirming"})
B2C_PRE_SOURCES = frozenset({"paid"})
ACTIVE = (*PRE_SOURCES, "pre_departure", "on_trip")
TARGET_LABEL = {"pre_departure": "Pre-departure", "on_trip": "On trip", "completed": "Completed"}
REASON_LABEL = {
    "not_booked": "suppliers not all confirmed",
    "not_paid": "not paid in full",
    "no_start_date": "no start date",
    "no_end_date": "no end date",
    "refused": "refused by the database guard",
}


# ── Settings ─────────────────────────────────────────────────────────────────


def _int_setting(raw: Any, key: str) -> int:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        value = None
    low, high = LIMITS[key]
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    return DEFAULTS[key]


async def policy(conn: asyncpg.Connection | asyncpg.Pool) -> dict[str, int]:
    rows = await conn.fetch("SELECT key, value FROM booking.settings WHERE key = ANY($1::text[])", list(DEFAULTS))
    raw = {r["key"]: r["value"] for r in rows}
    return {key: _int_setting(raw.get(key), key) for key in DEFAULTS}


async def ops_timezone(conn: asyncpg.Connection) -> str:
    """The ops time zone (``b2b_ops_timezone``, AA-974), falling back to Asia/Ho_Chi_Minh on a bad value."""
    raw = await conn.fetchval("SELECT value FROM booking.settings WHERE key = $1", TZ_SETTING)
    try:
        tz = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        tz = None
    if not isinstance(tz, str) or not tz or len(tz) > 64:
        return DEFAULT_TZ
    if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = $1)", tz):
        log.warning("ops_timezone_invalid value=%r", tz[:64])
        return DEFAULT_TZ
    return tz


async def ops_today(conn: asyncpg.Connection, tz: str) -> date:
    return await conn.fetchval("SELECT (now() AT TIME ZONE $1)::date", tz)


# ── The plan (pure) ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CaseFacts:
    id: str
    code: str
    status: str
    channel: str
    start_date: date | None
    end_date: date | None
    supply_state: str
    open_checks: int = 0


@dataclass
class Plan:
    moves: list[str] = field(default_factory=list)
    blocked: tuple[str, tuple[str, ...]] | None = None  # (target, reasons)


def plan(facts: CaseFacts, today: date, pre_departure_days: int) -> Plan:
    """The moves the facts allow today, in order, and the first gate that is due but blocked."""
    out = Plan()
    status = facts.status
    if facts.start_date is None:
        if status in PRE_SOURCES:
            out.blocked = ("pre_departure", ("no_start_date",))
        return out
    while True:
        if status in PRE_SOURCES:
            if facts.start_date - timedelta(days=pre_departure_days) > today:
                return out
            reasons = []
            if facts.supply_state not in ("booked", "none"):
                reasons.append("not_booked")
            sources = B2B_PRE_SOURCES if facts.channel == "b2b" else B2C_PRE_SOURCES
            if status not in sources:
                reasons.append("not_paid")
            if reasons:
                # Once the trip has started, the notice says so (a second, sharper notice).
                out.blocked = ("on_trip" if facts.start_date <= today else "pre_departure", tuple(reasons))
                return out
            status = "pre_departure"
        elif status == "pre_departure":
            if facts.start_date > today:
                return out
            status = "on_trip"
        elif status == "on_trip":
            if facts.end_date is None:
                if facts.start_date < today:
                    out.blocked = ("completed", ("no_end_date",))
                return out
            if facts.end_date >= today:
                return out
            status = "completed"
        else:
            return out
        out.moves.append(status)


# ── Mode ─────────────────────────────────────────────────────────────────────


def _case_ids(payload: dict[str, Any]) -> list[str] | None:
    raw = payload.get("case_ids")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw or len(raw) > MAX_CASE_IDS:
        raise PermanentError(f"case_ids must be a list of 1..{MAX_CASE_IDS} trip case ids")
    try:
        return sorted({str(uuid.UUID(str(v))) for v in raw})
    except ValueError as exc:
        raise PermanentError("case_ids must be trip case UUIDs") from exc


def decide_mode(payload: dict[str, Any], live_done: bool) -> tuple[str, str]:
    """-> (mode, reason); mode is 'dry_run' or 'live'. Pure."""
    if payload.get("trigger") == "schedule":
        if live_done:
            return "live", "scheduled run after an approved live run"
        return "dry_run", "no live run over every case yet (first runs are DRY RUN)"
    if payload.get("dry_run", True) is not False:
        return "dry_run", "manual dry run"
    return "live", "manual live run"


LIVE_DONE_SQL = """
SELECT EXISTS (
    SELECT 1 FROM booking.job
     WHERE kind = $1 AND status = 'succeeded' AND result ->> 'mode' = 'live' AND result ->> 'scoped' = 'false'
)
"""


# ── bk_case_lifecycle ────────────────────────────────────────────────────────

CANDIDATES_SQL = """
SELECT tc.id::text AS id FROM booking.trip_cases tc
 WHERE tc.deleted_at IS NULL AND tc.status = ANY($1::text[])
   AND (tc.start_date IS NULL OR tc.start_date - $2::int <= $3::date)
   AND tc.id > $4::uuid AND ($5::uuid[] IS NULL OR tc.id = ANY($5::uuid[]))
 ORDER BY tc.id
 LIMIT $6
"""
FACTS_SQL = """
SELECT tc.id::text AS id, tc.code, tc.status, tc.channel, tc.start_date, tc.end_date, s.state AS supply_state,
       (SELECT count(*) FROM booking.trip_case_checks k WHERE k.trip_case_id = tc.id AND k.status = 'open')::int
           AS open_checks
  FROM booking.trip_cases tc JOIN booking.v_trip_case_supply_state s ON s.trip_case_id = tc.id
 WHERE tc.id = ANY($1::uuid[])
 ORDER BY tc.id
"""
CLEAR_WHERE = """
    k.cleared_at IS NULL
    AND (k.emergency_contact_name IS NOT NULL OR k.emergency_contact_phone IS NOT NULL OR k.note IS NOT NULL)
    AND coalesce(tc.end_date, tc.start_date) + $1::int < $2::date
    AND ($3::uuid[] IS NULL OR tc.id = ANY($3::uuid[]))
"""
CLEAR_COUNT_SQL = f"""
SELECT count(*)::int AS checks, count(DISTINCT k.trip_case_id)::int AS cases
  FROM booking.trip_case_checks k JOIN booking.trip_cases tc ON tc.id = k.trip_case_id
 WHERE {CLEAR_WHERE}
"""  # noqa: S608 — constant fragment
CLEAR_SQL = f"""
WITH due AS (
    SELECT k.id FROM booking.trip_case_checks k JOIN booking.trip_cases tc ON tc.id = k.trip_case_id
     WHERE {CLEAR_WHERE}
     ORDER BY k.id
     LIMIT $4
     FOR UPDATE OF k SKIP LOCKED
)
UPDATE booking.trip_case_checks k
   SET emergency_contact_name = NULL, emergency_contact_phone = NULL, note = NULL, cleared_at = now()
  FROM due WHERE k.id = due.id
RETURNING k.trip_case_id::text AS trip_case_id
"""  # noqa: S608 — constant fragment


def _facts(row: Any) -> CaseFacts:
    return CaseFacts(
        id=row["id"],
        code=row["code"],
        status=row["status"],
        channel=row["channel"],
        start_date=row["start_date"],
        end_date=row["end_date"],
        supply_state=row["supply_state"],
        open_checks=row["open_checks"],
    )


class StaleCase(Exception):
    """The case changed status between the read and the write (another writer won)."""


def gate_key(case_id: str, target: str, reasons: tuple[str, ...]) -> str:
    return f"{GATE_TYPE}:{case_id}:{target}:{'+'.join(sorted(reasons))}"


async def _notify_gate(
    conn: asyncpg.Connection, facts: CaseFacts, target: str, reasons: tuple[str, ...], status: str
) -> bool:
    why = ", ".join(REASON_LABEL.get(r, r) for r in reasons)
    return await notifications.notify_staff_once(
        conn,
        type=GATE_TYPE,
        title=f"{facts.code} cannot move to {TARGET_LABEL[target]}: {why}"[:200],
        message=f"The travel date for {TARGET_LABEL[target]} is reached but the case is {status.replace('_', ' ')}"
        f" ({why}). Open checks: {facts.open_checks}.",
        entity_type="trip_case",
        entity_id=facts.id,
        reminder_key=gate_key(facts.id, target, reasons),
        data={"code": facts.code, "target": target, "reasons": list(reasons), "open_checks": facts.open_checks},
    )


async def _move(conn: asyncpg.Connection, facts: CaseFacts, before: str, target: str, today: date) -> None:
    note = f"Auto: travel dates ({KIND}, {today.isoformat()})"
    await conn.execute(
        "SELECT set_config('booking.actor', '', true), set_config('booking.status_note', $1, true)", note
    )
    done = await conn.execute(
        "UPDATE booking.trip_cases SET status = $3 WHERE id = $1::uuid AND status = $2", facts.id, before, target
    )
    await conn.execute("SELECT set_config('booking.status_note', '', true)")
    if done != "UPDATE 1":
        raise StaleCase(f"trip case {facts.code} is no longer {before}")
    await audit.record(
        conn,
        actor_type="system",
        actor_ref=ACTOR,
        action="trip_case.status_changed",
        entity_type="trip_case",
        entity_id=facts.id,
        before={"status": before},
        after={"status": target, "cause": "travel dates"},
        entity_label=facts.code,
        summary=f"{before} -> {target} (automatic: travel dates, {today.isoformat()})",
    )


@dataclass
class Tally:
    moves: dict[str, int] = field(default_factory=lambda: {t: 0 for t in TARGET_LABEL})
    moved_cases: list[dict[str, Any]] = field(default_factory=list)
    blocked: int = 0
    blocked_cases: list[dict[str, Any]] = field(default_factory=list)
    refused: int = 0
    notified: int = 0
    candidates: int = 0
    open_checks_at_move: int = 0

    def moved(self, facts: CaseFacts, steps: list[str]) -> None:
        for step in steps:
            self.moves[step] += 1
        if len(self.moved_cases) < MAX_LISTED:
            self.moved_cases.append({"code": facts.code, "from": facts.status, "to": steps})
        self.open_checks_at_move += facts.open_checks

    def gate(self, facts: CaseFacts, target: str, reasons: tuple[str, ...], status: str | None = None) -> None:
        self.blocked += 1
        if len(self.blocked_cases) < MAX_LISTED:
            self.blocked_cases.append(
                {"code": facts.code, "status": status or facts.status, "target": target, "reasons": list(reasons)}
            )


async def _live_case(conn: asyncpg.Connection, facts: CaseFacts, steps: Plan, today: date, tally: Tally) -> None:
    """Apply the planned moves one by one, each in a savepoint: a refusal keeps the moves made before it and turns the
    refused target into a blocked gate."""
    status, done = facts.status, []
    blocked = steps.blocked
    for target in steps.moves:
        try:
            async with conn.transaction():
                await _move(conn, facts, status, target, today)
        except (asyncpg.CheckViolationError, StaleCase) as exc:
            log.warning(
                "case_lifecycle_refused case=%s %s->%s: %s", facts.code, status, target, str(exc).split("\n")[0]
            )
            tally.refused += 1
            blocked = (target, ("refused",))
            break
        done.append(target)
        status = target
    if done:
        tally.moved(facts, done)
    if blocked:
        tally.gate(facts, *blocked, status)
        if await _notify_gate(conn, facts, *blocked, status):
            tally.notified += 1


async def run_lifecycle(
    pool: asyncpg.Pool,
    *,
    mode: str,
    case_ids: list[str] | None = None,
    today: date | None = None,
    batch: int = BATCH,
    max_batches: int = MAX_BATCHES,
) -> dict[str, Any]:
    """One pass over the open sold cases: plan (and, live, apply) the travel moves; count (and, live, clear) the
    emergency contacts past their keep window. ``today`` defaults to the ops date."""
    async with pool.acquire() as conn:
        tz = await ops_timezone(conn)
        rules = await policy(conn)
        if today is None:
            today = await ops_today(conn, tz)
    tally = Tally()
    after = "00000000-0000-0000-0000-000000000000"
    live = mode == "live"
    for _ in range(max_batches):
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('TimeZone', $1, true)", tz)
            ids = [
                r["id"]
                for r in await conn.fetch(
                    CANDIDATES_SQL + (" FOR UPDATE OF tc SKIP LOCKED" if live else ""),
                    list(ACTIVE),
                    rules["pre_departure_days"],
                    today,
                    after,
                    case_ids,
                    batch,
                )
            ]
            if not ids:
                break
            after = ids[-1]
            tally.candidates += len(ids)
            for row in await conn.fetch(FACTS_SQL, ids):
                facts = _facts(row)
                steps = plan(facts, today, rules["pre_departure_days"])
                if live:
                    await _live_case(conn, facts, steps, today, tally)
                    continue
                if steps.moves:
                    tally.moved(facts, steps.moves)
                if steps.blocked:
                    tally.gate(facts, *steps.blocked)
        if len(ids) < batch:
            break
    cleared = await _clear(
        pool, live=live, case_ids=case_ids, today=today, keep_days=rules["emergency_contact_keep_days"]
    )
    return {
        "mode": mode,
        "today": today.isoformat(),
        "timezone": tz,
        "scoped": case_ids is not None,
        "candidates": tally.candidates,
        "moves": tally.moves,
        "moved_cases": tally.moved_cases,
        "blocked": tally.blocked,
        "blocked_cases": tally.blocked_cases,
        "refused": tally.refused,
        "notified": tally.notified,
        "open_checks_at_move": tally.open_checks_at_move,
        **cleared,
    }


async def _clear(
    pool: asyncpg.Pool, *, live: bool, case_ids: list[str] | None, today: date, keep_days: int
) -> dict[str, int]:
    """DM-7: the emergency contact and the notes of the checks of a trip that ended ``keep_days`` ago."""
    if not live:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(CLEAR_COUNT_SQL, keep_days, today, case_ids)
        return {"cleared_checks": row["checks"], "cleared_cases": row["cases"]}
    checks, cases = 0, set()
    for _ in range(MAX_BATCHES):
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(CLEAR_SQL, keep_days, today, case_ids, BATCH)
            touched = sorted({r["trip_case_id"] for r in rows} - cases)
            for case_id in touched:
                await audit.record(
                    conn,
                    actor_type="system",
                    actor_ref=ACTOR,
                    action="trip_check.personal_data_cleared",
                    entity_type="trip_case",
                    entity_id=case_id,
                    after={"fields": ["emergency_contact_name", "emergency_contact_phone", "note"]},
                    summary=f"Trip ended more than {keep_days} days ago: emergency contact and check notes cleared",
                )
        checks += len(rows)
        cases.update(touched)
        if len(rows) < BATCH:
            break
    return {"cleared_checks": checks, "cleared_cases": len(cases)}


async def handle_lifecycle(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    payload = job.payload or {}
    case_ids = _case_ids(payload)
    async with pool.acquire() as conn:
        live_done = bool(await conn.fetchval(LIVE_DONE_SQL, KIND))
    mode, reason = decide_mode(payload, live_done)
    result = await run_lifecycle(pool, mode=mode, case_ids=case_ids)
    result.update({"reason": reason, "trigger": payload.get("trigger", "manual")})
    log.info(
        "case_lifecycle mode=%s moves=%s blocked=%s refused=%s cleared=%s",
        mode,
        result["moves"],
        result["blocked"],
        result["refused"],
        result["cleared_checks"],
    )
    return result


# ── bk_ops_reminders ─────────────────────────────────────────────────────────

WAITING_SQL = """
SELECT sb.id::text AS id, sb.code, sb.requested_at, sb.requested_by, tc.id::text AS case_id, tc.code AS case_code,
       s.company_name AS supplier_name, (st.active IS TRUE) AS requester_active
  FROM booking.supplier_bookings sb
  JOIN booking.trip_cases tc ON tc.id = sb.trip_case_id
  JOIN booking.suppliers s ON s.id = sb.supplier_id
  LEFT JOIN booking.staff_users st ON st.cis_user_id = sb.requested_by
 WHERE sb.status = 'requested' AND sb.requested_at < now() - make_interval(days => $1)
   AND tc.deleted_at IS NULL AND tc.status NOT IN ('cancelled', 'completed')
 ORDER BY sb.requested_at
 LIMIT $2
"""
CHECKS_DUE_SQL = """
SELECT k.id::text AS id, k.code, k.title, k.due_date, k.assignee_ref, (st.active IS TRUE) AS assignee_active,
       tc.id::text AS case_id, tc.code AS case_code
  FROM booking.trip_case_checks k
  JOIN booking.trip_cases tc ON tc.id = k.trip_case_id
  LEFT JOIN booking.staff_users st ON st.cis_user_id = k.assignee_ref
 WHERE k.status = 'open' AND k.due_date IS NOT NULL AND k.due_date <= $1::date
   AND tc.deleted_at IS NULL AND tc.status = ANY($2::text[])
 ORDER BY k.due_date, k.id
 LIMIT $3
"""
PAYMENTS_DUE_SQL = """
SELECT s.schedule_id::text AS id, s.seq, s.kind, s.remaining_amount, s.due_date, s.state, s.days_overdue,
       i.id::text AS invoice_id, i.invoice_number, i.currency, i.customer_id::text AS customer_id,
       tc.id::text AS case_id, tc.code AS case_code,
       (SELECT a.staff_user_id FROM booking.staff_assignments a
          JOIN booking.staff_users su ON su.cis_user_id = a.staff_user_id
         WHERE a.trip_case_id = tc.id AND a.role = 'sales' AND a.unassigned_at IS NULL AND su.active
         ORDER BY a.assigned_at DESC LIMIT 1) AS owner
  FROM booking.v_payment_schedule_status s
  JOIN booking.invoices i ON i.id = s.invoice_id
  JOIN booking.trip_cases tc ON tc.id = i.trip_case_id
 WHERE s.state IN ('due', 'overdue') AND i.deleted_at IS NULL AND i.status IN ('sent', 'partially_paid', 'paid')
   AND i.customer_id IS NOT NULL AND tc.deleted_at IS NULL AND tc.status <> 'cancelled'
 ORDER BY s.due_date, s.seq
 LIMIT $1
"""
CHECK_LABEL = {
    "pre_trip_recheck": "Re-check bookings",
    "emergency_contact": "Emergency contact",
    "insurance": "Insurance",
}


def overdue_step(days_overdue: int | None, steps: tuple[int, ...]) -> int | None:
    """The highest reminder step an overdue row has reached (None before the first)."""
    if days_overdue is None:
        return None
    reached = [s for s in steps if days_overdue >= s]
    return max(reached) if reached else None


def overdue_steps(raw: Any) -> tuple[int, ...]:
    """``payment_overdue_reminder_days``: a list of 1..365 day counts; the default [1, 7] on a bad value."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        value = None
    if (
        isinstance(value, list)
        and value
        and all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 365 for v in value)
    ):
        return tuple(sorted(set(value)))
    return DEFAULT_OVERDUE_STEPS


async def _payment_reminders(conn: asyncpg.Connection, limit: int) -> dict[str, int]:
    """S3: due-soon and overdue schedule rows -> one staff and one customer row each (deduped by reminder key)."""
    steps = overdue_steps(
        await conn.fetchval("SELECT value FROM booking.settings WHERE key = 'payment_overdue_reminder_days'")
    )
    rows = await conn.fetch(PAYMENTS_DUE_SQL, limit)
    counts = {"payments_due_soon": 0, "payments_overdue": 0, "payments_notified": 0, "customer_rows": 0}
    for r in rows:
        amount = f"{r['remaining_amount']} {r['currency'].strip()}"
        when = r["due_date"].isoformat()
        if r["state"] == "due":
            counts["payments_due_soon"] += 1
            kind, key = DUE_SOON_TYPE, f"{DUE_SOON_TYPE}:{r['id']}:{when}"
            staff_title = f"{r['case_code']}: {amount} due {when} on {r['invoice_number']}"
            customer_title = f"Payment due {when}"
            customer_message = f"{amount} is due on {when} for payment request {r['invoice_number']}."
        else:
            step = overdue_step(r["days_overdue"], steps)
            if step is None:
                continue
            counts["payments_overdue"] += 1
            kind, key = OVERDUE_TYPE, f"{OVERDUE_TYPE}:{r['id']}:{when}:d{step}"
            staff_title = f"{r['case_code']}: {amount} overdue since {when} on {r['invoice_number']}"
            customer_title = "Payment overdue"
            customer_message = (
                f"{amount} for payment request {r['invoice_number']} was due on {when}. Please contact your trip"
                " consultant if it is already on its way."
            )
        data = {"invoice_number": r["invoice_number"], "case_code": r["case_code"], "seq": r["seq"], "due_date": when}
        if await notifications.notify_staff_once(
            conn,
            type=kind,
            title=staff_title[:200],
            message="Remind the customer or record the payment on the invoice.",
            entity_type="invoice",
            entity_id=r["invoice_id"],
            reminder_key=key,
            data={**data, "case_id": r["case_id"]},
            recipient_ref=r["owner"],
        ):
            counts["payments_notified"] += 1
        if await notifications.notify_customer_once(
            conn,
            customer_id=r["customer_id"],
            type=kind,
            title=customer_title,
            message=customer_message,
            entity_type="invoice",
            entity_id=r["invoice_id"],
            reminder_key=key,
            data=data,
        ):
            counts["customer_rows"] += 1
    return counts


async def run_reminders(pool: asyncpg.Pool, *, today: date | None = None, limit: int = 1000) -> dict[str, Any]:
    async with pool.acquire() as conn:
        tz = await ops_timezone(conn)
        rules = await policy(conn)
        if today is None:
            today = await ops_today(conn, tz)
    waiting = due = 0
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('TimeZone', $1, true)", tz)
        rows = await conn.fetch(WAITING_SQL, rules["supplier_answer_days"], limit)
        for r in rows:
            asked = r["requested_at"].astimezone(UTC)
            if await notifications.notify_staff_once(
                conn,
                type=WAITING_TYPE,
                title=f"{r['case_code']}: {r['supplier_name']} has not answered {r['code']}"[:200],
                message=f"Requested {asked:%Y-%m-%d}, more than {rules['supplier_answer_days']} day(s) ago. Chase the"
                " supplier or book another one.",
                entity_type="supplier_booking",
                entity_id=r["id"],
                reminder_key=f"{WAITING_TYPE}:{r['id']}:{asked:%Y%m%dT%H%M%S}",
                data={"code": r["code"], "case_code": r["case_code"], "case_id": r["case_id"]},
                recipient_ref=r["requested_by"] if r["requester_active"] else None,
            ):
                waiting += 1
        horizon = today + timedelta(days=rules["trip_check_reminder_days"])
        checks = await conn.fetch(CHECKS_DUE_SQL, horizon, list(ACTIVE), limit)
        for k in checks:
            what = k["title"] if k["code"] == "custom" else CHECK_LABEL[k["code"]]
            late = k["due_date"] < today
            if await notifications.notify_staff_once(
                conn,
                type=CHECK_DUE_TYPE,
                title=f"{k['case_code']}: {what[:120]} {'is overdue' if late else 'is due soon'}"[:200],
                message=f"Due {k['due_date'].isoformat()}. Mark it done or not applicable on the case's Checks tab.",
                entity_type="trip_case",
                entity_id=k["case_id"],
                reminder_key=f"{CHECK_DUE_TYPE}:{k['id']}:{k['due_date'].isoformat()}",
                data={"case_code": k["case_code"], "check_id": k["id"], "check_code": k["code"]},
                recipient_ref=k["assignee_ref"] if k["assignee_active"] else None,
            ):
                due += 1
        payments = await _payment_reminders(conn, limit)
    return {
        "today": today.isoformat(),
        "timezone": tz,
        "supplier_waiting": len(rows),
        "supplier_waiting_notified": waiting,
        "checks_due": len(checks),
        "checks_due_notified": due,
        **payments,
    }


async def handle_reminders(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    result = await run_reminders(pool)
    result["trigger"] = (job.payload or {}).get("trigger", "manual")
    log.info(
        "ops_reminders waiting=%s checks_due=%s payments=%s customer_rows=%s",
        result["supplier_waiting_notified"],
        result["checks_due_notified"],
        result["payments_notified"],
        result["customer_rows"],
    )
    return result


HANDLERS = {KIND: handle_lifecycle, REMINDERS_KIND: handle_reminders}


# ── Schedule and enqueue ─────────────────────────────────────────────────────


def schedule_key(kind: str, day: date) -> str:
    return f"{kind}:{day.isoformat()}"


class OpsDailySchedule:
    """Worker maintenance hook: queue one ``trigger=schedule`` run of each kind per UTC day, on the first pass at or
    after :data:`SCHEDULE_UTC_HOUR`. Idempotent across workers and restarts (key per kind and day); the in-memory day
    only saves the INSERT on later passes."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_day: date | None = None

    async def __call__(self, pool: asyncpg.Pool) -> list[str]:
        now = self._now().astimezone(UTC)
        today = now.date()
        if now.hour < SCHEDULE_UTC_HOUR or self._done_day == today:
            return []
        queued = []
        for kind in KINDS:
            job_id = await queue.enqueue(
                pool,
                kind,
                {"trigger": "schedule"},
                idempotency_key=schedule_key(kind, today),
                max_attempts=MAX_ATTEMPTS,
                created_by=SCHEDULER,
            )
            if job_id:
                queued.append(job_id)
        self._done_day = today
        if queued:
            log.info("ops: queued %d scheduled runs for %s", len(queued), today)
        return queued


RUNS_SQL = """
SELECT id::text AS id, kind, status, created_by, created_at, started_at, finished_at, attempt, result::text AS result,
       error
  FROM booking.job WHERE kind = ANY($1::text[])
 ORDER BY created_at DESC
 LIMIT $2
"""


async def recent_runs(conn: asyncpg.Connection, limit: int = 20) -> dict[str, Any]:
    """The latest runs of both kinds (status, mode, counts): what the lifecycle did or would do."""
    rows = await conn.fetch(RUNS_SQL, list(KINDS), limit)
    items = []
    for r in rows:
        item = dict(r)
        try:
            item["result"] = json.loads(r["result"]) if r["result"] else None
        except ValueError:
            item["result"] = None
        item["error"] = (r["error"] or "")[:300] or None
        items.append(item)
    live_done = bool(await conn.fetchval(LIVE_DONE_SQL, KIND))
    return {
        "items": items,
        "scheduled_mode": "live" if live_done else "dry_run",
        "schedule_utc_hour": SCHEDULE_UTC_HOUR,
    }
