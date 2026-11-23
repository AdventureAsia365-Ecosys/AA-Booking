"""A scripted stand-in for an asyncpg pool/connection: answers by SQL substring, records calls.

Real SQL behaviour is covered by tests/integration (Postgres). These fakes only drive the Python
branches around the SQL.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Any

Answer = Any | Callable[..., Any]

# AA-1091 (bk-050): the pricing policy in force, as app.pricing.policy_values.IN_FORCE_SQL answers it. Every scripted
# connection answers it by default (lowest priority), so the code paths that read the policy work in every unit test;
# a test that needs other values scripts "FROM booking.pricing_policy" itself. Values: the bk-050 and bk-056 seeds.
POLICY_SEED: dict[str, str] = {
    "tariff_year_start": '"11-01"',
    "tariff_next_publish_by": '"08-31"',
    "quote_validity_days": "14",
    "quote_validity_festive_days": "3",
    "surcharge_causes": '["fx", "tax"]',
    "surcharge_fx_threshold_pct": '"5"',
    "surcharge_notice_days": "20",
    "surcharge_free_cancel_pct": '"8"',
    "recompute_auto_publish_pct": '"3"',
    "diff_highlight_pct": '"10"',
    "provisional_uplift_pct": '"5"',
    "provisional_cis_ref_quotable": "false",
    "group_on_request_min_pax": "9",
    "foc_one_per_pax": "15",
    "fit_full_payment_days": "30",
    "group_deposit_pct": '"25"',
    "group_deposit_days": "56",
    "festive_non_refundable_days": "30",
    "gala_dinner_days": '["12-24", "12-31"]',
    "agency_currency_by_country": '{"*": "USD", "VN": "VND"}',
    # S279 L3 (bk-056)
    "b2b_hold_hours": "48",
    "b2b_hold_extend_hours": "72",
    "b2b_hold_max_extensions": "1",
    "b2b_rehold_limit_per_day": "3",
    "b2b_on_request_answer_business_days": "1",
    "b2b_on_request_due_soon_hours": "4",
    "b2b_child_age_max": "11",
    "b2b_infant_age_max": "1",
    "departure_decision_days_default": "21",
    "pre_departure_days": "14",
    "supplier_answer_days": "2",
    "trip_check_reminder_days": "3",
    "supplier_bill_due_soon_days": "3",
    # AA-1128 B1 (bk-059)
    "season_sync_batch_size": "50",
    "season_sync_read_timeout_seconds": "5",
    "emergency_contact_keep_days": "90",
    "agency_reminder_steps_days": "[1, 7, 14]",
    "rounding_increment_by_currency": '{"AUD": "5", "EUR": "5", "GBP": "5", "SGD": "5", "USD": "5"}',
    "retention_job_succeeded_days": "30",
    "retention_job_failed_days": "90",
    "retention_inbox_redact_days": "90",
    # AA-1145 (bk-060)
    "child_price_pct_by_service_type": '{"activity": "100", "entrance": "100", "guide": "100", "meal": "100",'
    ' "transfer": "100"}',
    # AA-1111 S2 (bk-061)
    "staff_admin_mfa_required": "false",
    # AA-1171 (bk-063)
    "ops_jobs_stuck_after_s": "900",
    "ops_jobs_overdue_factor": '"2"',
    # AA-1149 (bk-065)
    "operable_exception_default_days": "14",
    "operable_exception_max_days": "30",
}
POLICY_FRAGMENT = "FROM booking.pricing_policy p JOIN booking.pricing_policy_events a"
# The seeded rounding increments (bk-056), as app.pricing.service.rounding_increments returns them; for the pure
# engine calls of the unit tests.
SEED_ROUNDING: dict[str, Decimal] = {
    code: Decimal(amount) for code, amount in json.loads(POLICY_SEED["rounding_increment_by_currency"]).items()
}


def seed_values(*keys: str) -> dict[str, Any]:
    """The seeded policy values of ``keys``, decoded (key -> value)."""
    return {key: json.loads(POLICY_SEED[key]) for key in keys}


def policy_rows(**overrides: str) -> list[dict[str, Any]]:
    """Rows of the policy in force: the seed, with ``overrides`` (key -> JSON text of the value)."""
    values = {**POLICY_SEED, **overrides}
    return [{"param_key": k, "value": v, "version_no": 1, "id": f"policy-{k}"} for k, v in sorted(values.items())]


class ScriptedConn:
    def __init__(self, rules: list[tuple[str, Answer]] | None = None) -> None:
        self.rules = list(rules or []) + [(POLICY_FRAGMENT, lambda *_: policy_rows())]
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.timeouts: list[tuple[str, float]] = []

    def on(self, fragment: str, answer: Answer) -> ScriptedConn:
        self.rules.insert(0, (fragment, answer))
        return self

    def _answer(self, sql: str, args: tuple[Any, ...]) -> Any:
        flat = " ".join(sql.split())
        self.calls.append((flat, args))
        for fragment, answer in self.rules:
            if fragment in flat:
                value = answer(*args) if callable(answer) else answer
                if isinstance(value, BaseException):
                    raise value
                return value
        return None

    def sql(self, fragment: str) -> list[tuple[Any, ...]]:
        """Args of every recorded call whose SQL contains ``fragment``."""
        return [args for flat, args in self.calls if fragment in flat]

    async def execute(self, sql: str, *args: Any) -> str:
        self._answer(sql, args)
        return "OK"

    async def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        for row in rows:
            self._answer(sql, row)

    async def fetchval(self, sql: str, *args: Any, **kw: Any) -> Any:
        self._timeout(sql, kw.get("timeout"))
        return self._answer(sql, args)

    async def fetchrow(self, sql: str, *args: Any, **kw: Any) -> Any:
        self._timeout(sql, kw.get("timeout"))
        return self._answer(sql, args)

    async def fetch(self, sql: str, *args: Any, **kw: Any) -> Any:
        self._timeout(sql, kw.get("timeout"))
        return self._answer(sql, args) or []

    def _timeout(self, sql: str, timeout: float | None) -> None:
        """AA-1128: the per-call ``timeout`` of a read (asyncpg's keyword), recorded with its SQL."""
        if timeout is not None:
            self.timeouts.append((" ".join(sql.split()), timeout))

    @asynccontextmanager
    async def transaction(self, **_: Any):
        yield self

    # Pool interface: the same object hands itself out.
    @asynccontextmanager
    async def acquire(self):
        yield self


# ── AA-986 L5: the served state of catalog keys (app.catalog.liveness) ─────────

PLAN_KEYS_FRAGMENT = "WHERE id = ANY($1::uuid[]) AND source_system = 'cis'"
STATES_FRAGMENT = "FROM unnest($1::text[]) AS k(source_id)"
VIEW_READY_FRAGMENT = "FROM pg_class c JOIN pg_namespace n"


def state_row(
    source_id: str,
    *,
    in_catalog: bool = True,
    tomb_state: str | None = None,
    tomb_reason: str | None = None,
    tomb_changed_at: Any = None,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "in_catalog": in_catalog,
        "tomb_state": tomb_state,
        "tomb_reason": tomb_reason,
        "tomb_changed_at": tomb_changed_at,
    }


def served_plan(conn: ScriptedConn, plan_id: str, source_id: str, **state: Any) -> ScriptedConn:
    """Script the B2B served check for one ``tp:`` plan (default: in ``catalog.v_tour``, no tombstone)."""
    conn.on(PLAN_KEYS_FRAGMENT, [{"id": plan_id, "source_id": source_id}])
    conn.on(STATES_FRAGMENT, [state_row(source_id, **state)])
    return conn


# ── AA-1149: the can-operate check of a tp: plan (app.catalog.operability) ─────

OPERABLE_PLANS_FRAGMENT = "no_hotel_nights FROM booking.trip_plans"
OPERABLE_COMPONENTS_FRAGMENT = "su.status AS supplier_status"
OPERABLE_EXCEPTIONS_FRAGMENT = "FROM booking.trip_plan_operable_overrides o"
PLAN_DAYS_FRAGMENT = "coalesce(max(c.day_no), 1) AS days"
TOUR_DAYS_FRAGMENT = "source_id, duration_days FROM catalog.v_tour"
SPANS_FRAGMENT = "AS stop FROM booking.supplier_rates x JOIN booking.supplier_contracts k"


def operable_plan(
    conn: ScriptedConn,
    plan_id: str,
    source_id: str,
    *,
    days: int,
    components: list[tuple[int, str | None, str | None, str | None]],
    no_hotel_nights: tuple[int, ...] = (),
    supplier_status: str = "active",
    exception: dict[str, Any] | None = None,
    rated: bool = True,
) -> ScriptedConn:
    """Script the can-operate check of one plan: ``components`` = ``(day, room type id, service id, service type)``,
    every item active, of supplier ``SUP-000001`` (``supplier_status``), rated for ever unless ``rated`` is False."""
    conn.on(OPERABLE_PLANS_FRAGMENT, [{"plan_id": plan_id, "source_id": source_id, "no_hotel_nights": no_hotel_nights}])
    conn.on(PLAN_DAYS_FRAGMENT, [{"id": plan_id, "source_id": source_id, "days": 1}])
    conn.on(TOUR_DAYS_FRAGMENT, [{"source_id": source_id, "duration_days": days}])
    conn.on(OPERABLE_EXCEPTIONS_FRAGMENT, [exception] if exception else [])
    rows = [
        {
            "plan_id": plan_id,
            "day_no": day,
            "room_type_id": room,
            "service_id": service,
            "status": "active",
            "hotel_status": "active",
            "code": "HTL-000001" if room else "SVC-000001",
            "room_type": "Deluxe" if room else None,
            "service_type": service_type,
            "destination_id": None,
            "supplier_code": "SUP-000001",
            "supplier_status": supplier_status,
        }
        for day, room, service, service_type in components
    ]
    conn.on(OPERABLE_COMPONENTS_FRAGMENT, rows)
    spans = [
        {
            "room_type_id": room,
            "service_id": service,
            "currency": "USD",
            "start": date(2000, 1, 1),
            "stop": date(9999, 12, 31),
        }
        for _, room, service, _ in components
    ]
    conn.on(SPANS_FRAGMENT, spans if rated else [])
    return conn
