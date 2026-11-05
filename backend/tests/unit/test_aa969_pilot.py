"""AA-969 part 2 (root ADR 0019 d16) without a database: the pilot tenant read and the retention predicate that leaves
the pilot tenant's rows out. On PostgreSQL: tests/integration/test_aa969_pilot_skip.py."""

from __future__ import annotations

import uuid

import pytest

from app import pilot
from app.catalog import liveness
from app.retention import policy, runner
from tests.unit.fakes import ScriptedConn

PILOT = uuid.UUID("00000000-0000-4000-8000-00000000f11e")


def test_uuid_array_literal_takes_validated_ids_only() -> None:
    assert pilot.uuid_array_literal([PILOT, str(PILOT)]) == f"'{{{PILOT},{PILOT}}}'::uuid[]"
    assert pilot.uuid_array_literal([]) == "'{}'::uuid[]"
    with pytest.raises(ValueError):
        pilot.uuid_array_literal(["x'); DROP TABLE booking.job; --"])


def test_without_pilots_adds_the_guard_to_tenant_tables_only() -> None:
    assert policy.without_pilots(policy.INBOX_SPEC, []) is policy.INBOX_SPEC
    assert policy.without_pilots(policy.JOB_SPEC, [PILOT]) is policy.JOB_SPEC  # booking.job holds no tenant data
    spec = policy.without_pilots(policy.INBOX_SPEC, [PILOT])
    (part,) = spec.parts
    assert part.window_days == 90 and spec.windows() == policy.INBOX_SPEC.windows()  # windows unchanged (DM-7.5)
    assert part.where.startswith("(t.redacted_at IS NULL AND t.status <> 'received') AND NOT EXISTS")
    assert f"pc.party_tenant_id = ANY('{{{PILOT}}}'::uuid[])" in part.where
    assert "pc.inbox_event_id = t.id" in runner.count_sql(spec, part, [])


@pytest.mark.asyncio
async def test_pilot_tenants_reads_the_view_or_nothing() -> None:
    liveness.reset_cache()
    conn = ScriptedConn().on("has_schema_privilege", True).on("WHERE is_pilot", [{"tenant_id": PILOT}])
    assert await pilot.pilot_tenants(conn) == [PILOT]  # type: ignore[arg-type]
    liveness.reset_cache()
    missing = ScriptedConn().on("has_schema_privilege", False)
    assert await pilot.pilot_tenants(missing) == []  # type: ignore[arg-type]
    assert not missing.sql("WHERE is_pilot")
    liveness.reset_cache()
