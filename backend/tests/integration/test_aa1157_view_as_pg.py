"""AA-1157 (root ADR 0019 d5 as amended) over HTTP on a real PostgreSQL, as role ``booking``: the owner's read-only
"view as a lower role".

- An owner (the CIS marker through the stub of ``shared.v_staff_booking_grant``) starts a view on the seeded "Product,
  ops & accounting" custom role: one ``staff.view_as_started`` row in ``booking.audit_logs`` (read back through the
  entity index) whose ``after`` holds the role's full capability list; reads run as that role (its permissions only, no
  staff page); a write is refused 403 before the route and writes nothing; Back ends it with one
  ``staff.view_as_ended`` row and the session stops working (409).
- A new start ends the previous view (``superseded``); sign-out's end-all ends the open one.
- An administrator who is not an owner cannot start one (403 ``owner_only``).
All names and subs are invented.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.auth import custom_roles, view_as
from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

OWNER = "a1157000-0000-4000-8000-0000000000c1"
ADMIN = "a1157000-0000-4000-8000-0000000000c2"
TOTP = ["pwd", "otp", "mfa"]
SEED = custom_roles.SEED_CODE


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)

    async def clean() -> None:
        await conn.execute("DELETE FROM shared.staff_booking_grant_stub WHERE cognito_sub LIKE 'a1157000-%'")
        await conn.execute("DELETE FROM booking.audit_logs WHERE actor_ref LIKE 'cog:a1157000-%'")
        await conn.execute("DELETE FROM booking.staff_users WHERE cognito_sub LIKE 'a1157000-%'")
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


async def grant(admin: asyncpg.Connection, sub: str, *, owner: bool) -> None:
    await admin.execute(
        "INSERT INTO shared.staff_booking_grant_stub (cognito_sub, booking_role_code, is_owner) VALUES ($1, NULL, $2)"
        " ON CONFLICT (cognito_sub) DO UPDATE SET is_owner = EXCLUDED.is_owner",
        sub,
        owner,
    )
    custom_roles.forget()


def as_admin(cis: LocalCis, sub: str, view: str | None = None) -> dict[str, str]:
    h = {"Authorization": "Bearer " + cis.workforce.token(["booking-admin", "cis-admin"], sub=sub, amr=TOTP)}
    if view:
        h[view_as.HEADER] = view
    return h


async def rows(admin: asyncpg.Connection, session_id: str) -> list[dict]:
    got = await admin.fetch(
        "SELECT action, actor_type, after::text AS after FROM booking.audit_logs "
        "WHERE entity_type = 'staff_view_as' AND entity_id = $1::uuid "
        "ORDER BY created_at, (action = 'staff.view_as_ended')",
        session_id,
    )
    return [{"action": r["action"], "actor_type": r["actor_type"], "after": json.loads(r["after"])} for r in got]


async def test_an_owner_views_as_a_custom_role_read_only(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    client, cis = http
    await grant(admin, OWNER, owner=True)
    started = await client.post("/booking/view-as", json={"role": SEED, "minutes": 15}, headers=as_admin(cis, OWNER))
    assert started.status_code == 201, started.text
    sid = started.json()["session_id"]
    (start_row,) = await rows(admin, sid)
    assert start_row["action"] == view_as.STARTED
    assert start_row["after"]["capabilities"] == sorted(custom_roles.SEED_CAPABILITIES)
    assert start_row["after"]["viewer_role"] == "admin" and start_row["after"]["target_role"] == SEED

    me = await client.get("/booking/staff/me", headers=as_admin(cis, OWNER, sid))
    assert me.status_code == 200, me.text
    assert set(me.json()["permissions"]) == custom_roles.SEED_CAPABILITIES and me.json()["read_only"] is True
    assert (await client.get("/booking/suppliers", headers=as_admin(cis, OWNER, sid))).status_code == 200
    assert (await client.get("/booking/staff", headers=as_admin(cis, OWNER, sid))).status_code == 403
    write = await client.post(
        "/booking/suppliers", json={"company_name": "zz1157 Harbor Kayaks", "categories": ["transport"]},
        headers=as_admin(cis, OWNER, sid),
    )  # fmt: skip
    assert (write.status_code, write.json()["detail"]) == (403, view_as.READ_ONLY)
    assert not await admin.fetchval("SELECT count(*) FROM booking.suppliers WHERE company_name LIKE 'zz1157 %'")

    ended = await client.post(f"/booking/view-as/{sid}/end", headers=as_admin(cis, OWNER))
    assert ended.status_code == 200 and ended.json()["changed"] is True
    assert [r["action"] for r in await rows(admin, sid)] == [view_as.STARTED, view_as.ENDED]
    gone = await client.get("/booking/staff/me", headers=as_admin(cis, OWNER, sid))
    assert (gone.status_code, gone.json()["detail"]) == (409, view_as.ENDED_DETAIL)


async def test_a_new_start_supersedes_and_sign_out_ends(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    client, cis = http
    await grant(admin, OWNER, owner=True)
    first = (await client.post("/booking/view-as", json={"role": "sales"}, headers=as_admin(cis, OWNER))).json()
    second = (await client.post("/booking/view-as", json={"role": "finance"}, headers=as_admin(cis, OWNER))).json()
    first_rows = await rows(admin, first["session_id"])
    assert [r["after"].get("end_reason") for r in first_rows if r["action"] == view_as.ENDED] == ["superseded"]
    out = await client.post("/booking/view-as/end-all", headers=as_admin(cis, OWNER))
    assert out.json()["ended"] == [second["session_id"]]


async def test_an_admin_who_is_not_an_owner_cannot_start(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    client, cis = http
    await grant(admin, ADMIN, owner=False)
    r = await client.post("/booking/view-as", json={"role": "sales"}, headers=as_admin(cis, ADMIN))
    assert (r.status_code, r.json()["detail"]) == (403, view_as.OWNER_ONLY)
    assert not await admin.fetchval(
        "SELECT count(*) FROM booking.audit_logs WHERE entity_type = 'staff_view_as' AND actor_ref = $1", f"cog:{ADMIN}"
    )
