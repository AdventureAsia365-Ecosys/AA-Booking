"""bk_tenant_pull without a database (AA-1028 R1 slice 2): the projection rules, the audit actions, the run on a
scripted connection (not configured, no view, create / adopt / update / unchanged / gone / internal / awaiting
CIS), the schedule and
the staff edit guard on a projected row. The same rules on PostgreSQL: tests/integration/test_aa1028_tenant_pull.py."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.b2b import partners, tenant_pull
from app.catalog import liveness
from app.errors import Conflict
from app.jobs.kinds import EXPECTED_SECONDS
from app.jobs.queue import Job
from tests.unit.fakes import VIEW_READY_FRAGMENT, ScriptedConn

CLIENT = "crmClient1028"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
T1, T2, T3, T4, T5 = (uuid.UUID(f"00000000-0000-4000-8000-00000000000{i}") for i in range(1, 6))


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    liveness.reset_cache()


def account(tenant: uuid.UUID, name: str = "Zephyr Trails", *, in_service: bool = True, trade_book: bool = True,
            kind: str = "agency") -> dict[str, Any]:  # fmt: skip
    return {"tenant_id": tenant, "name": name, "in_service": in_service, "kind": kind, "trade_book": trade_book}


def partner(tenant: uuid.UUID, *, name: str = "Zephyr Trails", status: str = "active", client: str = CLIENT,
            projected: bool = True) -> dict[str, Any]:  # fmt: skip
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, str(tenant))), "tenant_id": tenant, "agency_name": name,
            "status": status, "m2m_client_id": client, "projected_at": NOW if projected else None}  # fmt: skip


def conn(accounts: list[dict[str, Any]], partners_rows: list[dict[str, Any]], *, ready: bool = True) -> ScriptedConn:
    c = ScriptedConn()
    c.on(VIEW_READY_FRAGMENT, ready)
    c.on("FROM catalog.v_tenant_account_v1", accounts)
    c.on("FROM booking.b2b_partners FOR UPDATE", partners_rows)
    c.on("INSERT INTO booking.b2b_partners", lambda *a: str(uuid.uuid4()))
    return c


def test_desired_row() -> None:
    assert tenant_pull.desired(account(T1, "  Zephyr \n Trails  "), CLIENT) == {
        "agency_name": "Zephyr Trails", "status": "active", "m2m_client_id": CLIENT}  # fmt: skip
    assert tenant_pull.desired(account(T1, in_service=False), CLIENT)["status"] == "suspended"
    assert tenant_pull.desired(account(T1, trade_book=False), CLIENT)["status"] == "suspended"
    assert tenant_pull.desired(account(T1, "x" * 300), CLIENT)["agency_name"] == "x" * 200
    assert tenant_pull.desired(account(T1, " "), CLIENT)["agency_name"] == "Unnamed agency"


def test_audit_actions() -> None:
    row = partner(T1)
    assert tenant_pull._action(row, {**tenant_pull.desired(account(T1), CLIENT), "status": "suspended"}) == (
        "b2b_partner.suspended")  # fmt: skip
    assert tenant_pull._action(partner(T1, status="suspended"), tenant_pull.desired(account(T1), CLIENT)) == (
        "b2b_partner.reactivated")  # fmt: skip
    assert tenant_pull._action(row, tenant_pull.desired(account(T1, "New name"), CLIENT)) == "b2b_partner.projected"


@pytest.mark.asyncio
async def test_not_configured_and_no_view() -> None:
    c = conn([], [], ready=False)
    assert (await tenant_pull.run(c, ""))["outcome"] == "not_configured"  # type: ignore[arg-type]
    assert (await tenant_pull.run(c, CLIENT))["outcome"] == "skipped"  # type: ignore[arg-type]
    assert c.sql("FROM catalog.v_tenant_account_v1") == []
    assert await tenant_pull.TenantPullSchedule(lambda: NOW)(c) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_projects_every_tenant() -> None:
    accounts = [
        account(T1),  # new
        account(T2, "Larkspur Journeys"),  # unchanged
        account(T3, in_service=False),  # CIS suspended it
        account(T4, kind="internal"),  # AA itself: never a row
        account(T5, "Quillfeather Tours"),  # a staff row the pull has not projected yet
    ]
    rows = [
        partner(T2, name="Larkspur Journeys"),
        partner(T3, name="Zephyr Trails"),
        partner(T5, name="Quillfeather Tours", projected=False),
        partner(uuid.UUID(int=99)),  # gone from CIS
        partner(uuid.UUID(int=98), status="suspended"),  # gone and already suspended
        partner(uuid.UUID(int=97), projected=False),  # a staff row with no CIS tenant: left alone (adoption guard)
        partner(uuid.UUID(int=96), status="suspended", projected=False),  # the same, already suspended by staff
    ]
    c = conn(accounts, rows)
    handler = tenant_pull.build_handler(CLIENT)
    out = await handler(c, Job("j", tenant_pull.KIND, {}, 1, 3))  # type: ignore[arg-type]
    assert out == {"outcome": "ok", "tenants": 5, "created": 1, "updated": 2, "suspended_gone": 1, "unchanged": 1,
                   "internal_skipped": 1, "awaiting_cis": 2, "erased_skipped": 0}  # fmt: skip
    assert c.sql("INSERT INTO booking.b2b_partners") == [(T1, "Zephyr Trails", "active", "USD", CLIENT)]
    updates = c.sql("UPDATE booking.b2b_partners")
    assert (partner(T3)["id"], "Zephyr Trails", "suspended", CLIENT) in updates
    assert (partner(T5)["id"], "Quillfeather Tours", "active", CLIENT) in updates  # adopted
    assert (partner(uuid.UUID(int=99))["id"], "Zephyr Trails", "suspended", CLIENT) in updates
    assert not [u for u in updates if u[0] in {partner(uuid.UUID(int=97))["id"], partner(uuid.UUID(int=96))["id"]}]
    assert len(updates) == 3
    actions = [args[3] for args in c.sql("INSERT INTO booking.audit_logs")]
    assert sorted(actions) == ["b2b_partner.created", "b2b_partner.projected", "b2b_partner.suspended",
                               "b2b_partner.suspended"]  # fmt: skip


@pytest.mark.asyncio
async def test_an_erased_agency_is_never_projected_again() -> None:
    """AA-1093 L3: an agency Booking erased (an applied bk-057 log row) keeps its erased name while CIS still holds it,
    and is not suspended again once CIS drops it."""
    erased_name = f"Erased agency {str(T1)[:8]}"
    rows = [partner(T1, name=erased_name, status="suspended"), partner(T2, name=f"Erased agency {str(T2)[:8]}")]
    c = conn([account(T1, "Zephyr Trails")], rows)
    c.on("to_regclass('booking.tenant_erasure_log')", True)
    c.on("FROM booking.tenant_erasure_log WHERE mode = 'apply'", [{"tenant_id": T1}, {"tenant_id": T2}])
    out = await tenant_pull.run(c, CLIENT)  # type: ignore[arg-type]
    assert out["erased_skipped"] == 1 and out["updated"] == 0 and out["suspended_gone"] == 0
    assert c.sql("UPDATE booking.b2b_partners") == [] and c.sql("INSERT INTO booking.b2b_partners") == []


@pytest.mark.asyncio
async def test_a_concurrent_insert_is_left_to_the_next_run() -> None:
    c = conn([account(T1)], []).on("INSERT INTO booking.b2b_partners", None)
    out = await tenant_pull.run(c, CLIENT)  # type: ignore[arg-type]
    assert out["created"] == 0 and c.sql("INSERT INTO booking.audit_logs") == []


@pytest.mark.asyncio
async def test_schedule_one_run_per_slot() -> None:
    c = conn([], []).on("INSERT INTO booking.job", "job-1")
    clock = {"now": NOW}
    hook = tenant_pull.TenantPullSchedule(lambda: clock["now"])
    await hook(c)  # type: ignore[arg-type]
    assert await hook(c) is None  # type: ignore[arg-type]
    clock["now"] = NOW + timedelta(seconds=tenant_pull.SLOT_SECONDS)
    await hook(c)  # type: ignore[arg-type]
    assert len(c.sql("INSERT INTO booking.job")) == 2
    assert tenant_pull.schedule_key(NOW).startswith("bk_tenant_pull:slot:")
    assert tenant_pull.KIND in EXPECTED_SECONDS


@pytest.mark.asyncio
async def test_staff_cannot_change_the_projected_fields_of_a_projected_row() -> None:
    staff = SimpleNamespace(cis_user_id="staff-1")
    row = {**partner(T1), "billing_currency": "USD", "credit_terms_ref": None, "created_by": None,
           "updated_by": None, "created_at": NOW, "updated_at": NOW, "tenant_id": str(T1)}  # fmt: skip
    c = ScriptedConn().on("FOR UPDATE", row)
    with pytest.raises(Conflict) as info:
        await partners.update_partner(c, staff, row["id"], {"status": "suspended"})  # type: ignore[arg-type]
    assert info.value.code == "projected_from_cis" and "status" in info.value.message
    c.on("UPDATE booking.b2b_partners", {**row, "credit_terms_ref": "NET-30"})
    out = await partners.update_partner(c, staff, row["id"], {"credit_terms_ref": "NET-30"})  # type: ignore[arg-type]
    assert out["credit_terms_ref"] == "NET-30"
    # a row the pull never projected stays editable
    c = ScriptedConn().on("FOR UPDATE", {**row, "projected_at": None})
    c.on("UPDATE booking.b2b_partners", {**row, "status": "suspended", "projected_at": None})
    assert (await partners.update_partner(c, staff, row["id"], {"status": "suspended"}))["status"] == "suspended"  # type: ignore[arg-type]
