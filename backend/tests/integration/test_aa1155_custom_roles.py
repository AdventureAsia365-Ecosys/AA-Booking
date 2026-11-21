"""AA-1155 T5 / T6 (root spec R2.2, R2.3, R4.1-R4.4) over HTTP on a real PostgreSQL, as role ``booking``.

The CIS contract view ``shared.v_staff_booking_grant`` is the CI stub of ``stub_shared.sql`` (same columns and grant as
CIS 462); the tests fill its stub table as ``aa_cis_admin``.

- A ``booking-custom`` holder whose grant names the seeded "Product, ops & accounting" role gets exactly its
  capabilities: its staff row holds the custom code, it reads suppliers, supplier bills and agency invoices (200), a
  supplier it makes starts ``inactive`` and every approval it tries is 403 ``forbidden``.
- With SELECT on the view or on ``booking.staff_role`` revoked from ``booking``, the custom holder is refused (403
  ``custom_role_unavailable``) and a built-in administrator still opens ``/booking/staff`` (R4.2). An archived role and
  a holder with no grant are refused (403 ``no_booking_role``).
- The role editor: an ``is_owner`` administrator clones a built-in role and ticks capabilities (audited before and
  after; the next request of a holder sees the change); an approve-class or unknown capability is 400 and writes
  nothing; an administrator who is not ``is_owner`` is 403 ``owner_only``, and so is an owner while the view is
  unreadable.
All names, subs and codes are invented.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.auth import custom_roles
from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

HOLDER = "a1155000-0000-4000-8000-0000000000c1"
OWNER = "a1155000-0000-4000-8000-0000000000c2"
ADMIN = "a1155000-0000-4000-8000-0000000000c3"
STRANGER = "a1155000-0000-4000-8000-0000000000c4"
SEED = custom_roles.SEED_CODE
ZZ = "custom_zz_quay_desk"
ROW = "a1155000-0000-4000-8000-0000000000ff"
TOTP = ["pwd", "otp", "mfa"]


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)

    async def clean() -> None:
        await conn.execute("GRANT SELECT ON shared.v_staff_booking_grant TO booking")
        await conn.execute("SET ROLE bk_owner")
        try:
            await conn.execute("GRANT SELECT ON booking.staff_role, booking.staff_role_capability TO booking")
            await conn.execute("DELETE FROM booking.suppliers WHERE company_name LIKE 'zz1155 %'")
            await conn.execute("DELETE FROM booking.staff_role WHERE code LIKE 'custom_zz%'")
            await conn.execute("UPDATE booking.staff_role SET archived = false WHERE code = $1", SEED)
        finally:
            await conn.execute("RESET ROLE")
        await conn.execute("DELETE FROM shared.staff_booking_grant_stub WHERE cognito_sub LIKE 'a1155000-%'")
        await conn.execute("DELETE FROM booking.staff_users WHERE cognito_sub LIKE 'a1155000-%'")
        custom_roles.forget()

    await clean()
    try:
        yield conn
    finally:
        await clean()
        await conn.close()


@pytest_asyncio.fixture
async def http(migrated_booking_dsn: str) -> AsyncIterator[tuple[httpx.AsyncClient, LocalCis]]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking") as client:
            yield client, cis
    finally:
        await db.close()


async def grant(admin: asyncpg.Connection, sub: str, code: str | None, *, owner: bool = False) -> None:
    await admin.execute(
        "INSERT INTO shared.staff_booking_grant_stub (cognito_sub, booking_role_code, is_owner) VALUES ($1, $2, $3)"
        " ON CONFLICT (cognito_sub) DO UPDATE SET booking_role_code = EXCLUDED.booking_role_code,"
        " is_owner = EXCLUDED.is_owner",
        sub,
        code,
        owner,
    )
    custom_roles.forget()


def holder(cis: LocalCis, sub: str = HOLDER) -> dict[str, str]:
    return {"Authorization": "Bearer " + cis.workforce.token(["cis-custom", "booking-custom"], sub=sub)}


def built_in_admin(cis: LocalCis, sub: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + cis.workforce.token(["booking-admin", "cis-admin"], sub=sub, amr=TOTP)}


def refused(r: httpx.Response, detail: str) -> None:
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == detail, r.text


# ── R4.1 / R4.3: the seeded role through the view ────────────────────────────


async def test_a_custom_holder_works_with_exactly_its_role(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    client, cis = http
    await grant(admin, HOLDER, SEED)
    me = await client.get("/booking/staff/me", headers=holder(cis))
    assert me.status_code == 200, me.text
    assert me.json()["role"] == SEED and me.json()["role_label"] == custom_roles.SEED_LABEL
    assert set(me.json()["permissions"]) == custom_roles.SEED_CAPABILITIES
    assert await admin.fetchval("SELECT role FROM booking.staff_users WHERE cognito_sub = $1", HOLDER) == SEED
    # The Staff page names the custom role (list and profile).
    listed = await client.get("/booking/staff", headers=built_in_admin(cis, ADMIN))
    row = next(r for r in listed.json()["items"] if r["cis_user_id"] == f"cog:{HOLDER}")
    assert (row["role"], row["role_label"]) == (SEED, custom_roles.SEED_LABEL)
    profile = await client.get(f"/booking/staff/cog:{HOLDER}", headers=holder(cis))
    assert profile.status_code == 200 and profile.json()["role_label"] == custom_roles.SEED_LABEL

    # The pages of the live check: suppliers, supplier bills (/billing), agency invoices (/agency-billing).
    for path in ("/booking/suppliers", "/booking/supplier-bills", "/booking/agency-invoices"):
        r = await client.get(path, headers=holder(cis))
        assert r.status_code == 200, (path, r.text)

    # A maker: its supplier starts inactive, and activating it is an approval it does not hold.
    made = await client.post(
        "/booking/suppliers", json={"company_name": "zz1155 Lagoon Boats", "categories": ["transport"]},
        headers=holder(cis),
    )  # fmt: skip
    assert made.status_code == 201, made.text
    assert made.json()["status"] == "inactive"
    refused(await client.post(f"/booking/suppliers/{made.json()['id']}/activate", headers=holder(cis)), "forbidden")
    assert await admin.fetchval("SELECT status FROM booking.suppliers WHERE id = $1::uuid", made.json()["id"]) == (
        "inactive"
    )

    # Approvals (R4.3), every one refused before the record is read (no such row exists).
    for path, body in (
        (f"/booking/pricing/rules/{ROW}/approve", None),
        (f"/booking/pricing/fx-rates/{ROW}/approve", None),
        (f"/booking/pricing/cancellation-policies/{ROW}/approve", None),
        (f"/booking/pricing/partner-tier-discounts/{ROW}/approve", None),
        (f"/booking/pricing/partner-rate-cards/{ROW}/approve", None),
        (f"/booking/b2b-desk/price-adjustments/{ROW}/approve", None),
        (f"/booking/catalog/allotments/{ROW}/approve", None),
        (f"/booking/supply/stop-sales/{ROW}/decision", {"action": "approve"}),
        (f"/booking/supply/stop-sales/{ROW}/end", None),
        (f"/booking/supplier-bills/{ROW}/approve", None),
        (f"/booking/supplier-bills/{ROW}/payouts", {}),
        ("/booking/billing-entities", {}),
    ):
        refused(await client.post(path, json=body, headers=holder(cis)), "forbidden")
    refused(
        await client.patch(f"/booking/contracts/{ROW}", json={"status": "published"}, headers=holder(cis)), "forbidden"
    )
    refused(await client.get("/booking/staff", headers=holder(cis)), "forbidden")


# ── R4.2: fail closed for custom holders only ────────────────────────────────


@pytest.mark.parametrize(
    "revoke",
    [
        "REVOKE SELECT ON shared.v_staff_booking_grant FROM booking",
        "REVOKE SELECT ON booking.staff_role, booking.staff_role_capability FROM booking",
    ],
)
async def test_a_revoked_grant_refuses_the_custom_holder_and_keeps_the_administrator(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection, revoke: str
) -> None:
    client, cis = http
    await grant(admin, HOLDER, SEED)
    assert (await client.get("/booking/staff/me", headers=holder(cis))).status_code == 200
    if "booking.staff_role" in revoke:
        await admin.execute("SET ROLE bk_owner")
        await admin.execute(revoke)
        await admin.execute("RESET ROLE")
    else:
        await admin.execute(revoke)
    custom_roles.forget()
    refused(await client.get("/booking/staff/me", headers=holder(cis)), "custom_role_unavailable")
    staff = await client.get("/booking/staff", headers=built_in_admin(cis, ADMIN))
    assert staff.status_code == 200, staff.text


async def test_an_archived_role_or_no_grant_is_no_booking_role(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    client, cis = http
    refused(await client.get("/booking/staff/me", headers=holder(cis, STRANGER)), "no_booking_role")
    await grant(admin, HOLDER, SEED)
    await admin.execute("SET ROLE bk_owner")
    await admin.execute("UPDATE booking.staff_role SET archived = true WHERE code = $1", SEED)
    await admin.execute("RESET ROLE")
    custom_roles.forget()
    refused(await client.get("/booking/staff/me", headers=holder(cis)), "no_booking_role")
    assert await admin.fetchval("SELECT count(*) FROM booking.staff_users WHERE cognito_sub LIKE 'a1155000-%'") == 0


# ── T6 / R2.2 / R4.4: the role editor ────────────────────────────────────────


async def _audit(admin: asyncpg.Connection, action: str) -> list[asyncpg.Record]:
    return await admin.fetch(
        "SELECT before, after, summary FROM booking.audit_logs WHERE action = $1 AND entity_label LIKE $2",
        action,
        f"%({ZZ})",
    )


async def test_an_owner_edits_a_custom_role_and_a_holder_follows(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    client, cis = http
    await grant(admin, OWNER, None, owner=True)
    owner = built_in_admin(cis, OWNER)

    listed = await client.get("/booking/staff-roles", headers=owner)
    assert listed.status_code == 200 and listed.json()["can_edit"] is True
    assert SEED in {r["code"] for r in listed.json()["custom"]}

    made = await client.post(
        "/booking/staff-roles", json={"code": ZZ, "label": "Quay desk", "based_on": "sales"}, headers=owner
    )
    assert made.status_code == 201, made.text
    sales = next(b for b in listed.json()["builtin"] if b["code"] == "sales")
    clone = {c for c in sales["capabilities"] if not custom_roles.is_approve_class(c)}
    assert set(made.json()["capabilities"]) == clone
    stored = await admin.fetch("SELECT capability FROM booking.staff_role_capability WHERE role_code = $1", ZZ)
    assert {r["capability"] for r in stored} == clone
    (created,) = await _audit(admin, "staff_role.created")
    assert created["before"] is None and json.loads(created["after"])["capabilities"] == sorted(clone)

    # A holder of the new role, then a tick: the editor drops the cache, the next request follows.
    await grant(admin, HOLDER, ZZ)
    me = await client.get("/booking/staff/me", headers=holder(cis))
    assert set(me.json()["permissions"]) == clone
    ticked = await client.patch(
        f"/booking/staff-roles/{ZZ}", json={"capabilities": ["supply.read", "bill.read"]}, headers=owner
    )
    assert ticked.status_code == 200 and ticked.json()["capabilities"] == ["bill.read", "supply.read"]
    me = await client.get("/booking/staff/me", headers=holder(cis))
    assert me.json()["permissions"] == ["bill.read", "supply.read"]
    (updated,) = await _audit(admin, "staff_role.updated")
    assert json.loads(updated["before"])["capabilities"] == sorted(clone)
    assert json.loads(updated["after"])["capabilities"] == ["bill.read", "supply.read"]

    # 400 before any write: an approval or a name the code does not know.
    for caps, detail in (
        (["supply.read", "supplier.activate"], "approve_class_capability"),
        (["x.y"], "unknown_capability"),
    ):
        r = await client.patch(f"/booking/staff-roles/{ZZ}", json={"capabilities": caps}, headers=owner)
        assert r.status_code == 400 and r.json()["detail"] == detail, r.text
    assert len(await _audit(admin, "staff_role.updated")) == 1

    archived = await client.patch(f"/booking/staff-roles/{ZZ}", json={"archived": True}, headers=owner)
    assert archived.status_code == 200 and archived.json()["archived"] is True
    refused(await client.get("/booking/staff/me", headers=holder(cis)), "no_booking_role")


async def test_only_an_owner_writes_and_an_unreadable_view_is_no_owner(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    client, cis = http
    body = {"code": ZZ, "label": "Quay desk", "based_on": "contracting"}
    await grant(admin, ADMIN, None, owner=False)
    plain = built_in_admin(cis, ADMIN)
    assert (await client.get("/booking/staff-roles", headers=plain)).json()["can_edit"] is False
    refused(await client.post("/booking/staff-roles", json=body, headers=plain), "owner_only")
    refused(await client.patch(f"/booking/staff-roles/{SEED}", json={"archived": True}, headers=plain), "owner_only")

    await grant(admin, OWNER, None, owner=True)
    await admin.execute("REVOKE SELECT ON shared.v_staff_booking_grant FROM booking")
    refused(await client.post("/booking/staff-roles", json=body, headers=built_in_admin(cis, OWNER)), "owner_only")
    assert await admin.fetchval("SELECT count(*) FROM booking.staff_role WHERE code = $1", ZZ) == 0


async def test_the_database_refuses_an_approval_the_editor_would_refuse(admin: asyncpg.Connection) -> None:
    """Defence in depth (R2.2): the bk-062 CHECK refuses what the editor refuses, if the code were bypassed."""
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(
            "INSERT INTO booking.staff_role (code, label, based_on, created_by, updated_by)"
            " VALUES ($1, 'Quay desk', 'sales', 'it-1155', 'it-1155')",
            ZZ,
        )
        for capability in ("pricing.approve", "supplier.activate", "payout.write", "staff.admin"):
            with pytest.raises(asyncpg.CheckViolationError):
                await admin.execute(
                    "INSERT INTO booking.staff_role_capability (role_code, capability, created_by)"
                    " VALUES ($1, $2, 'it-1155')",
                    ZZ,
                    capability,
                )
    finally:
        await admin.execute("RESET ROLE")
