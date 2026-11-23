"""AA-1149 (B13): the can-operate rules N1-N5, the sale exception's cover, the quote gate, the policy wiring and the
capability (``app.catalog.operability``, ``app.catalog.sell_exceptions``). Pure; the database side is in
``tests/integration/test_aa1149_tour_split.py``."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from app.auth import custom_roles
from app.auth.staff import PERMISSIONS
from app.b2b.errors import B2BError
from app.catalog import operability as op
from app.catalog import sell_exceptions
from app.errors import Invalid
from app.pricing import policy_params
from tests.unit.fakes import ScriptedConn, operable_plan

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
HOTEL_A, HOTEL_B, TRANSFER, GUIDE = "rt-a", "rt-b", "sv-transfer", "sv-guide"


def comp(day: int, *, room: str | None = None, service: str | None = None, **kw: Any) -> op.Comp:
    base: dict[str, Any] = {
        "active": True,
        "code": "HTL-000001" if room else "SVC-000001",
        "supplier_code": "SUP-000001",
        "supplier_status": "active",
    }
    if room:
        base |= {"room_type": "Deluxe", "destination_id": "dest-1"}
    if service == TRANSFER:
        base["service_type"] = "transfer"
    if service == GUIDE:
        base["service_type"] = "guide"
    return op.Comp(day_no=day, room_type_id=room, service_id=service, **(base | kw))


def five_days() -> list[op.Comp]:
    """The Done-when tour: a hotel on days 1-4, a transfer every day, a guide on day 3."""
    return [
        *(comp(d, room=HOTEL_A) for d in range(1, 5)),
        *(comp(d, service=TRANSFER) for d in range(1, 6)),
        comp(3, service=GUIDE),
    ]


def rated(components: list[op.Comp]) -> dict[str, set[str]]:
    return {c.item: {"USD"} for c in components}


def plan(components: list[op.Comp], *, days: int = 5, nights: frozenset[int] = frozenset(), **kw: Any) -> op.PlanFacts:
    return op.PlanFacts("plan-1", "src-1", days, nights, components, **kw)


def keys(verdict: op.Verdict) -> list[str]:
    return [f.key for f in verdict.findings]


def exception(findings: list[str], *, expires: datetime = NOW + timedelta(days=5)) -> op.LiveException:
    return op.LiveException("ex-1", ("N1",), frozenset(findings), expires, "reason", "admin", "Admin", NOW)


def test_a_complete_tour_with_one_guide_day_only_warns() -> None:
    parts = five_days()
    verdict = op.evaluate(plan(parts), rated(parts), NOW)
    assert verdict.status == op.WARNINGS
    assert keys(verdict) == ["N5:guide"]
    assert verdict.findings[0].detail == "a guide on some days but not on day 1, 2, 4, 5"


def test_n1_counts_days_minus_one_nights_and_skips_no_hotel_nights() -> None:
    parts = [c for c in five_days() if not (c.room_type_id and c.day_no == 2) and c.service_id != GUIDE]
    verdict = op.evaluate(plan(parts), rated(parts), NOW)
    assert verdict.status == op.BLOCKED and keys(verdict) == ["N1:night:2"]
    assert op.evaluate(plan(parts, nights=frozenset({2})), rated(parts), NOW).status == op.READY


def test_days_fall_back_to_the_components() -> None:
    parts = [comp(1, room=HOTEL_A), comp(3, service=TRANSFER)]
    assert op.days_of(None, parts) == 3
    assert op.days_of(5, parts) == 5
    assert op.days_of("x", []) == 1


def test_n2_and_n3_block_and_are_never_overridable() -> None:
    parts = [comp(1, room=HOTEL_A), comp(2, service=TRANSFER, active=False)]
    verdict = op.evaluate(plan(parts, days=2), {HOTEL_A: {"USD"}}, NOW)
    assert keys(verdict) == ["N2:day:2:SVC-000001", "N3:item:2:SVC-000001"]
    assert verdict.status == op.BLOCKED
    assert not any(f.overridable for f in verdict.findings)
    assert "has no published rate in the next 365 days" in verdict.findings[0].detail


def test_a_supplier_not_active_blocks_once_and_is_overridable() -> None:
    parts = [comp(1, room=HOTEL_A, supplier_status="pending"), comp(2, service=TRANSFER, supplier_status="pending")]
    verdict = op.evaluate(plan(parts, days=2), rated(parts), NOW)
    assert keys(verdict) == ["N3:supplier:SUP-000001"]
    assert verdict.findings[0].overridable and verdict.findings[0].detail.endswith("(pending)")


def test_n4_warns_on_a_destination_change_without_a_transfer() -> None:
    parts = [comp(1, room=HOTEL_A), comp(2, room=HOTEL_B, destination_id="dest-2"), comp(3, service=TRANSFER)]
    verdict = op.evaluate(plan(parts, days=3), rated(parts), NOW)
    assert keys(verdict) == ["N4:day:2"] and verdict.status == op.WARNINGS
    moved = [*parts, comp(2, service=TRANSFER)]
    assert op.evaluate(plan(moved, days=3), rated(moved), NOW).status == op.READY


def test_no_component_blocks() -> None:
    assert keys(op.evaluate(plan([], days=1), {}, NOW)) == ["N2:none"]


def test_an_exception_covers_its_findings_only_while_live() -> None:
    parts = [c for c in five_days() if not (c.room_type_id and c.day_no in (2, 3))]
    found = op.evaluate(plan(parts, exception=exception(["N1:night:2"])), rated(parts), NOW)
    assert found.status == op.BLOCKED  # night 3 is a new finding: never covered
    assert [f.key for f in found.blocking] == ["N1:night:3"]
    both = op.evaluate(plan(parts, exception=exception(["N1:night:2", "N1:night:3"])), rated(parts), NOW)
    assert both.status == op.EXCEPTION and both.covered == {"N1:night:2", "N1:night:3"}
    assert both.out()["exception_until"] == NOW + timedelta(days=5)
    expired = exception(["N1:night:2", "N1:night:3"], expires=NOW - timedelta(seconds=1))
    assert op.evaluate(plan(parts, exception=expired), rated(parts), NOW).status == op.BLOCKED


def test_an_exception_cannot_cover_a_missing_rate() -> None:
    parts = [comp(1, room=HOTEL_A)]
    verdict = op.evaluate(plan(parts, days=1, exception=exception(["N2:day:1:HTL-000001"])), {}, NOW)
    assert verdict.status == op.BLOCKED


def test_the_refusal_names_the_line_findings_and_rules() -> None:
    parts = [c for c in five_days() if not (c.room_type_id and c.day_no == 2)]
    error = op.refusal("T1", op.evaluate(plan(parts), rated(parts), NOW))
    assert error.error == "invalid"
    assert str(error.message) == "line T1: this tour cannot be sold to agencies yet: night 2 has no hotel (N1)"


@pytest.mark.asyncio
async def test_the_quote_gate_skips_departure_lines_and_returns_the_covering_exception() -> None:
    conn = ScriptedConn()
    operable_plan(conn, "plan-1", "src-1", days=3, components=[(1, HOTEL_A, None, None)])
    with pytest.raises(B2BError) as refused:
        await op.require_for_quote(conn, [("T", "plan-1", None)], on=date(2026, 11, 23), now=NOW)
    assert "night 2 has no hotel (N1)" in refused.value.message
    assert await op.require_for_quote(conn, [("T", "plan-1", "dep-1"), ("R", None, None)], on=NOW.date(), now=NOW) == [
        None,
        None,
    ]
    covered = {
        "id": "ex-9",
        "plan_id": "plan-1",
        "rules": ["N1"],
        "findings": ["N1:night:2"],
        "reason": "r",
        "expires_at": NOW + timedelta(days=1),
        "created_by": "a",
        "created_by_name": None,
        "created_at": NOW,
    }
    operable_plan(conn, "plan-1", "src-1", days=3, components=[(1, HOTEL_A, None, None)], exception=covered)
    assert await op.require_for_quote(conn, [("T", "plan-1", None)], on=NOW.date(), now=NOW) == ["ex-9"]


def test_overridable_lists_n1_and_supplier_findings_only() -> None:
    parts = [comp(1, room=HOTEL_A, supplier_status="inactive"), comp(3, service=TRANSFER, active=False)]
    verdict = op.evaluate(plan(parts, days=3), rated(parts), NOW)
    assert sell_exceptions.overridable(verdict) == {"N1": ["N1:night:2"], "N3": ["N3:supplier:SUP-000001"]}


def test_the_capability_is_admin_only_and_never_a_custom_role_right() -> None:
    assert PERMISSIONS["operable_exception.approve"] == frozenset({"admin"})
    assert custom_roles.is_approve_class("operable_exception.approve")
    assert "operable_exception.approve" not in custom_roles.assignable()


def test_policy_parameters_and_their_bounds() -> None:
    default = policy_params.PARAMS["operable_exception_default_days"]
    longest = policy_params.PARAMS["operable_exception_max_days"]
    assert (default.section, default.group, longest.section, longest.group) == (
        "operations",
        "admin",
        "operations",
        "admin",
    )
    with pytest.raises(policy_params.PolicyValueError):
        policy_params.normalise("operable_exception_default_days", 31, {"operable_exception_max_days": 30})
    with pytest.raises(policy_params.PolicyValueError):
        policy_params.normalise("operable_exception_max_days", 10, {"operable_exception_default_days": 14})
    assert policy_params.normalise("operable_exception_max_days", 30, {"operable_exception_default_days": 14}) == 30


# ── The staff side (app.catalog.sell_exceptions) on scripted SQL ────────────────────────────────────────────────


class _Staff:
    cis_user_id = "it-admin"


def _scripted(
    days: int = 3, components: list[tuple[int, str | None, str | None, str | None]] | None = None
) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM booking.trip_plans WHERE source_system = 'cis' AND source_id", {"id": "plan-1", "source_id": "src-1"})
    conn.on("SELECT (now() AT TIME ZONE 'UTC')::date", date(2026, 11, 23))
    conn.on("INSERT INTO booking.trip_plan_operable_overrides", "ex-new")
    operable_plan(conn, "plan-1", "src-1", days=days, components=components or [(1, HOTEL_A, None, None)])
    return conn


@pytest.mark.asyncio
async def test_operability_of_lists_what_an_exception_can_cover() -> None:
    out = await sell_exceptions.operability_of(_scripted(), "src-1")
    assert out["status"] == op.BLOCKED and out["days"] == 3
    assert out["overridable"] == {"N1": ["N1:night:2"]}
    assert (out["exception_default_days"], out["exception_max_days"]) == (14, 30)


@pytest.mark.asyncio
async def test_create_exception_checks_rules_reason_and_length() -> None:
    conn = _scripted()
    reason = "The guesthouse for night 2 is booked by hand."
    for kwargs, code in (
        ({"rules": ["N2"], "reason": reason, "days": None}, "rule_not_overridable"),
        ({"rules": ["N1"], "reason": "short", "days": None}, "reason_too_short"),
        ({"rules": ["N1"], "reason": reason, "days": 31}, "expiry_too_long"),
        ({"rules": ["N3"], "reason": reason, "days": None}, "rule_not_failing"),
    ):
        with pytest.raises(Invalid) as refused:
            await sell_exceptions.create_exception(conn, _Staff(), "src-1", **kwargs)  # type: ignore[arg-type]
        assert refused.value.code == code
    await sell_exceptions.create_exception(conn, _Staff(), "src-1", rules=["N1"], reason=reason, days=None)  # type: ignore[arg-type]
    [insert] = conn.sql("INSERT INTO booking.trip_plan_operable_overrides")
    assert insert[1:4] == (["N1"], ["N1:night:2"], reason)
    assert timedelta(days=13) < insert[4] - datetime.now(UTC) <= timedelta(days=14)
    assert conn.sql("UPDATE booking.trip_plan_operable_overrides SET revoked_by")  # the previous one is revoked first


@pytest.mark.asyncio
async def test_revoke_needs_a_live_exception_and_nights_stay_inside_the_tour() -> None:
    conn = _scripted()
    with pytest.raises(Exception, match="no sale exception"):
        await sell_exceptions.revoke_exception(conn, _Staff(), "src-1", reason="Hotel signed.")  # type: ignore[arg-type]
    conn.on("UPDATE booking.trip_plan_operable_overrides SET revoked_by", [{"id": "ex-1", "expires_at": NOW}])
    await sell_exceptions.revoke_exception(conn, _Staff(), "src-1", reason="Hotel signed.")  # type: ignore[arg-type]
    with pytest.raises(Exception, match="nights 1 to 2"):
        await sell_exceptions.set_no_hotel_nights(conn, _Staff(), "src-1", [3])  # type: ignore[arg-type]
    await sell_exceptions.set_no_hotel_nights(conn, _Staff(), "src-1", [2])  # type: ignore[arg-type]
    [update] = conn.sql("SET no_hotel_nights")
    assert update[1] == [2]
