"""Can a tour be operated: the B2B sale check of a trip plan (AA-1149 B13; S285 advisory r2 item B13, bk-065).

A tour sold to an agency must be bookable with AA suppliers night by night. The check reads the plan's components
(``booking.trip_plan_components``), its nights that need no hotel (``trip_plans.no_hotel_nights``), the published rate
spans of its items and its live sale exception, and gives findings:

- **N1** (blocks): every night ``n`` of the tour (``1 .. days - 1``; night ``n`` is between day ``n`` and day
  ``n + 1``) has a room component on day ``n``, unless the night is marked "no hotel needed" (a night train, a cruise
  not modelled as a room type). Key ``N1:night:<n>``.
- **N2** (blocks): every component has a published rate covering some day from today to 365 days ahead
  (``app.catalog.rate_horizon.rated_items`` on ``cost_sheet.SPANS_SQL``, so the readiness report and this check agree).
  This is "has a published rate in the next 365 days", not "the price is valid on every date": the quote checks the
  exact dates and refuses a day with no rate in force. At the quote gate the window reaches at least the end of each
  line's dates (``max(today + 365, service date + days)``), so a tour quoted for a season past the horizon is judged
  on that season's rates. Key ``N2:day:<d>:<code>``; never covered by an exception.
- **N3** (blocks): every component's supplier is ``active`` (a pending, inactive or any later suspended status
  blocks; key ``N3:supplier:<code>``, the only N3 finding an exception may cover) and its room type, hotel or service is
  active (key ``N3:item:<d>:<code>``; the quote refuses an inactive item whatever an exception says).
- **N4** (warns): the hotel's destination changes between night ``n`` and night ``n + 1`` and day ``n + 1`` has no
  ``transfer`` service. Key ``N4:day:<d>``.
- **N5** (warns): the tour has a guide on some days but not on every day. Key ``N5:guide``.
- A plan without a component: ``N2:none`` (blocks; the quote refuses it too).

**Days** = the larger of the catalog length (``catalog.v_tour.duration_days``), the highest component day and 1, the
rule of :mod:`app.b2b.plan_span`.

**Status**: ``blocked`` (a blocking finding no exception covers), ``exception`` (every blocking finding is covered by
the live sale exception), ``warnings`` (only warnings) or ``ready``. An exception (``trip_plan_operable_overrides``,
:mod:`app.catalog.sell_exceptions`) covers a finding only while it is live (not revoked, ``expires_at`` later than now)
and only when the finding's key is in its list: a new finding is never covered.

**The gate** (:func:`require_for_quote`): a ``tp:`` quote line without a departure (tailor-made) whose tour is
``blocked`` is refused 422 ``invalid`` naming the line and the findings; a line on a fixed departure is not checked
(the advisory blocks tailor-made B2B sales only). The quote is the only gate: placing and confirming a hold do not
check again. The ids of the exceptions that covered a line are returned for ``b2b_quote_lines.operable_override_id``.

Bounded: one query each for the plans, their components, their live exceptions and their items' rate spans (batched
by :data:`BATCH`), whatever the number of plans.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import asyncpg

from app.b2b import plan_span
from app.b2b.errors import B2BError
from app.catalog import cost_sheet
from app.catalog.rate_horizon import HORIZON_DAYS, horizon, rated_items

BLOCK = "block"
WARN = "warn"
READY = "ready"
WARNINGS = "warnings"
BLOCKED = "blocked"
EXCEPTION = "exception"
STATUSES: tuple[str, ...] = (READY, WARNINGS, BLOCKED, EXCEPTION)
OVERRIDABLE_RULES: tuple[str, ...] = ("N1", "N3")
OVERRIDABLE_PREFIXES: tuple[str, ...] = ("N1:night:", "N3:supplier:")
BATCH = 500
MAX_FINDINGS_OUT = 10  # technical-constant: how many findings a report row shows (the page links to all), not a policy

PLANS_SQL = """
SELECT id::text AS plan_id, source_id, no_hotel_nights_reason, no_hotel_nights FROM booking.trip_plans
WHERE id = ANY($1::uuid[])
"""
COMPONENTS_SQL = """
SELECT c.trip_plan_id::text AS plan_id, c.day_no, c.hotel_room_type_id::text AS room_type_id,
       c.service_id::text AS service_id, coalesce(rt.status, s.status) AS status,
       coalesce(h.status, 'active') AS hotel_status, coalesce(h.code, s.code) AS code, rt.name AS room_type,
       s.service_type, h.destination_id::text AS destination_id, su.code AS supplier_code,
       su.status AS supplier_status
FROM booking.trip_plan_components c
LEFT JOIN booking.hotel_room_types rt ON rt.id = c.hotel_room_type_id
LEFT JOIN booking.hotels h ON h.id = rt.hotel_id
LEFT JOIN booking.services s ON s.id = c.service_id
LEFT JOIN booking.suppliers su ON su.id = coalesce(h.supplier_id, s.supplier_id)
WHERE c.trip_plan_id = ANY($1::uuid[])
ORDER BY c.trip_plan_id, c.day_no, coalesce(c.hotel_room_type_id, c.service_id)
"""
EXCEPTIONS_SQL = """
SELECT o.id::text AS id, o.trip_plan_id::text AS plan_id, o.rules, o.findings, o.reason, o.expires_at, o.created_by,
       o.created_at, su.name AS created_by_name
FROM booking.trip_plan_operable_overrides o
LEFT JOIN booking.staff_users su ON su.cis_user_id = o.created_by
WHERE o.trip_plan_id = ANY($1::uuid[]) AND o.revoked_at IS NULL
"""


# ── Pure part ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    rule: str
    level: str
    key: str
    detail: str

    @property
    def overridable(self) -> bool:
        return self.key.startswith(OVERRIDABLE_PREFIXES)

    def out(self, covered: bool = False) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "level": self.level,
            "key": self.key,
            "detail": self.detail,
            "overridable": self.overridable,
            "covered": covered,
        }


@dataclass(frozen=True)
class Comp:
    day_no: int
    room_type_id: str | None
    service_id: str | None
    active: bool
    code: str | None
    room_type: str | None = None
    service_type: str | None = None
    destination_id: str | None = None
    supplier_code: str | None = None
    supplier_status: str | None = None

    @property
    def item(self) -> str:
        return self.room_type_id or self.service_id or ""

    @property
    def name(self) -> str:
        return f"{self.code} {self.room_type}" if self.room_type else str(self.code)


@dataclass(frozen=True)
class LiveException:
    id: str
    rules: tuple[str, ...]
    findings: frozenset[str]
    expires_at: datetime
    reason: str
    created_by: str
    created_by_name: str | None
    created_at: datetime

    def live(self, now: datetime) -> bool:
        return self.expires_at > now

    def out(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rules": list(self.rules),
            "findings": sorted(self.findings),
            "reason": self.reason,
            "expires_at": self.expires_at,
            "created_by": {"id": self.created_by, "name": self.created_by_name},
            "created_at": self.created_at,
        }


@dataclass
class PlanFacts:
    plan_id: str
    source_id: str
    days: int
    no_hotel_nights: frozenset[int] = frozenset()
    components: list[Comp] = field(default_factory=list)
    exception: LiveException | None = None
    rate_until: date | None = None  # the quote gate's N2 window end when past the horizon (the line's dates)
    no_hotel_nights_reason: str | None = None


@dataclass(frozen=True)
class Verdict:
    status: str
    findings: tuple[Finding, ...]
    covered: frozenset[str]
    exception: LiveException | None

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level == BLOCK and f.key not in self.covered)

    def out(self, limit: int | None = MAX_FINDINGS_OUT) -> dict[str, Any]:
        shown = self.findings if limit is None else self.findings[:limit]
        live = self.exception if self.covered else None
        return {
            "status": self.status,
            "findings": [f.out(f.key in self.covered) for f in shown],
            "findings_total": len(self.findings),
            "exception_until": live.expires_at if live else None,
        }


def days_of(duration_days: Any, components: Iterable[Comp]) -> int:
    """The tour's days: catalog length, highest component day, 1 (the :mod:`app.b2b.plan_span` rule)."""
    top = max((c.day_no for c in components), default=1)
    try:
        catalog_days = int(duration_days or 1)
    except (TypeError, ValueError):
        catalog_days = 1
    return min(max(catalog_days, top, 1), plan_span.MAX_DAYS)


def _n1(plan: PlanFacts) -> list[Finding]:
    hotel_days = {c.day_no for c in plan.components if c.room_type_id}
    return [
        Finding("N1", BLOCK, f"N1:night:{n}", f"night {n} has no hotel")
        for n in range(1, plan.days)
        if n not in hotel_days and n not in plan.no_hotel_nights
    ]


def _n2(plan: PlanFacts, rated: Mapping[str, Any]) -> list[Finding]:
    if not plan.components:
        return [Finding("N2", BLOCK, "N2:none", "the tour has no bookable components")]
    window = f"from today to {plan.rate_until.isoformat()}" if plan.rate_until else f"in the next {HORIZON_DAYS} days"
    return [
        Finding("N2", BLOCK, f"N2:day:{c.day_no}:{c.code}", f"day {c.day_no}: {c.name} has no published rate {window}")
        for c in plan.components
        if c.item not in rated
    ]


def _n3(plan: PlanFacts) -> list[Finding]:
    out: list[Finding] = []
    seen: set[str] = set()
    for c in plan.components:
        if c.supplier_status != "active" and c.supplier_code not in seen:
            seen.add(str(c.supplier_code))
            status = c.supplier_status or "missing"
            out.append(
                Finding(
                    "N3",
                    BLOCK,
                    f"N3:supplier:{c.supplier_code}",
                    f"supplier {c.supplier_code} is not active ({status})",
                )  # fmt: skip
            )
        if not c.active:
            out.append(Finding("N3", BLOCK, f"N3:item:{c.day_no}:{c.code}", f"day {c.day_no}: {c.name} is inactive"))
    return out


def _n4(plan: PlanFacts) -> list[Finding]:
    dest = {c.day_no: c.destination_id for c in plan.components if c.room_type_id and c.destination_id}
    transfers = {c.day_no for c in plan.components if c.service_type == "transfer"}
    return [
        Finding("N4", WARN, f"N4:day:{n + 1}", f"day {n + 1}: the hotel moves to another destination with no transfer")
        for n in range(1, plan.days)
        if n in dest and n + 1 in dest and dest[n] != dest[n + 1] and n + 1 not in transfers
    ]


def _n5(plan: PlanFacts) -> list[Finding]:
    guided = {c.day_no for c in plan.components if c.service_type == "guide"}
    missing = [d for d in range(1, plan.days + 1) if d not in guided]
    if not guided or not missing:
        return []
    days = ", ".join(str(d) for d in missing[:20])
    return [Finding("N5", WARN, "N5:guide", f"a guide on some days but not on day {days}")]


def findings(plan: PlanFacts, rated: Mapping[str, Any]) -> list[Finding]:
    return [*_n1(plan), *_n2(plan, rated), *_n3(plan), *_n4(plan), *_n5(plan)]


def evaluate(plan: PlanFacts, rated: Mapping[str, Any], now: datetime) -> Verdict:
    """The plan's status and findings (see the module doc)."""
    found = tuple(findings(plan, rated))
    live = plan.exception if plan.exception is not None and plan.exception.live(now) else None
    covered = frozenset(
        f.key for f in found if live is not None and f.level == BLOCK and f.overridable and f.key in live.findings
    )
    blocking = [f for f in found if f.level == BLOCK and f.key not in covered]
    if blocking:
        status = BLOCKED
    elif covered:
        status = EXCEPTION
    elif found:
        status = WARNINGS
    else:
        status = READY
    return Verdict(status, found, covered, plan.exception)


def refusal(line_ref: str, verdict: Verdict) -> B2BError:
    shown = verdict.blocking[:5]
    more = len(verdict.blocking) - len(shown)
    text = "; ".join(f"{f.detail} ({f.rule})" for f in shown) + (f"; and {more} more" if more > 0 else "")
    return B2BError("invalid", f"line {line_ref}: this tour cannot be sold to agencies yet: {text}")


# ── Database part ────────────────────────────────────────────────────────────


def _comp(row: Mapping[str, Any]) -> Comp:
    return Comp(
        day_no=int(row["day_no"]),
        room_type_id=row["room_type_id"],
        service_id=row["service_id"],
        active=cost_sheet.component_active(row),
        code=row["code"],
        room_type=row["room_type"],
        service_type=row["service_type"],
        destination_id=row["destination_id"],
        supplier_code=row["supplier_code"],
        supplier_status=row["supplier_status"],
    )


def _exception(row: Mapping[str, Any]) -> LiveException:
    return LiveException(
        id=row["id"],
        rules=tuple(row["rules"] or ()),
        findings=frozenset(row["findings"] or ()),
        expires_at=row["expires_at"],
        reason=row["reason"],
        created_by=row["created_by"],
        created_by_name=row["created_by_name"],
        created_at=row["created_at"],
    )


async def _spans(conn: asyncpg.Connection, plans: Iterable[PlanFacts]) -> list[Any]:
    """The published rate spans of the plans' items (batched)."""
    rooms = sorted({c.room_type_id for p in plans for c in p.components if c.room_type_id})
    services = sorted({c.service_id for p in plans for c in p.components if c.service_id})
    out: list[Any] = []
    for start in range(0, max(len(rooms), len(services)), BATCH):
        out.extend(
            await conn.fetch(cost_sheet.SPANS_SQL, rooms[start : start + BATCH], services[start : start + BATCH])
        )
    return out


async def rated_for(conn: asyncpg.Connection, plans: Iterable[PlanFacts], on: date) -> dict[str, set[str]]:
    """``{item id: currencies}`` of the plans' items with a published rate in the horizon."""
    return rated_items(await _spans(conn, list(plans)), on)


def quote_window(on: date, days: int, service_dates: Iterable[date]) -> date | None:
    """The N2 window end of a quote's lines of one tour: ``max(today + 365, service date + days)``; None when the
    horizon already covers every line."""
    end = max((d + timedelta(days=days) for d in service_dates), default=None)
    return end if end is not None and end > horizon(on) else None


async def load_many(
    conn: asyncpg.Connection, plan_ids: Sequence[str], *, duration: Mapping[str, Any] | None = None
) -> dict[str, PlanFacts]:
    """The facts of the given plans. ``duration``: ``{plan id: catalog duration_days}`` when the caller already read
    the catalog (the readiness report); else the days come from :func:`app.b2b.plan_span.plan_days`."""
    ids = sorted(set(plan_ids))
    if not ids:
        return {}
    out: dict[str, PlanFacts] = {}
    for start in range(0, len(ids), BATCH):
        batch = ids[start : start + BATCH]
        for row in await conn.fetch(PLANS_SQL, batch):
            nights = frozenset(int(n) for n in (row["no_hotel_nights"] or ()))
            out[row["plan_id"]] = PlanFacts(row["plan_id"], row["source_id"], 1, nights)
            out[row["plan_id"]].no_hotel_nights_reason = row["no_hotel_nights_reason"]
        for row in await conn.fetch(COMPONENTS_SQL, batch):
            out[row["plan_id"]].components.append(_comp(row))
        for row in await conn.fetch(EXCEPTIONS_SQL, batch):
            out[row["plan_id"]].exception = _exception(row)
    if duration is None:
        days, _ = await plan_span.plan_days(conn, list(out))
        for plan_id, plan in out.items():
            plan.days = days.get(plan_id, 1)
    else:
        for plan_id, plan in out.items():
            plan.days = days_of(duration.get(plan_id), plan.components)
    return out


async def verdicts(
    conn: asyncpg.Connection,
    plan_ids: Sequence[str],
    *,
    on: date,
    now: datetime,
    duration: Mapping[str, Any] | None = None,
    rated: Mapping[str, Any] | None = None,
) -> dict[str, Verdict]:
    """``{plan id: verdict}`` of the given plans (``rated`` reused when the caller already read the rate spans)."""
    plans = await load_many(conn, plan_ids, duration=duration)
    have = rated if rated is not None else await rated_for(conn, plans.values(), on)
    return {plan_id: evaluate(plan, have, now) for plan_id, plan in plans.items()}


async def require_for_quote(
    conn: asyncpg.Connection,
    lines: Sequence[tuple[str, str | None, str | None, date | None]],
    *,
    on: date,
    now: datetime,
) -> list[str | None]:
    """The gate of a quote: ``lines`` = ``(line_ref, tp: plan id or None, departure id or None, service date)`` per
    line. A tailor-made tp: line (no departure) of a blocked tour is refused 422 ``invalid``; N2 looks for rates up to
    the end of the tour's quoted dates when that is past the horizon (:func:`quote_window`). Returns per line the id of
    the sale exception that covered one of its findings (None otherwise)."""
    checked = sorted({plan for _, plan, dep, _ in lines if plan is not None and dep is None})
    if not checked:
        return [None] * len(lines)
    plans = await load_many(conn, checked)
    for plan_id, plan in plans.items():
        dates = [d for _, p, dep, d in lines if p == plan_id and dep is None and d is not None]
        plan.rate_until = quote_window(on, plan.days, dates)
    spans = await _spans(conn, plans.values())
    found = {pid: evaluate(plan, rated_items(spans, on, plan.rate_until), now) for pid, plan in plans.items()}
    out: list[str | None] = []
    for ref, plan, dep, _ in lines:
        verdict = found.get(plan) if plan is not None and dep is None else None
        if verdict is None:
            out.append(None)
            continue
        if verdict.status == BLOCKED:
            raise refusal(ref, verdict)
        out.append(verdict.exception.id if verdict.covered and verdict.exception else None)
    return out
