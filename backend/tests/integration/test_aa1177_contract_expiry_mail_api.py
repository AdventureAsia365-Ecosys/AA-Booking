"""AA-1177 (Jira PR-31, bk-074): supplier contract expiry reminders reach the admins, on a real PostgreSQL as role
``booking``.

Done-when of the issue, without SES: a synthetic hotel contract ending within the window is listed (every contract
kind, one supplier's list, the My work tile), the daily job announces it once and e-mails the active staff of the
policy roles once (a fake sender stands in for SES), a second run sends nothing, and changing the window
(``contract_expiry_alert_days``) changes what is listed. All names and addresses are invented."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app import mailer
from app.db import Database
from app.jobs.queue import Job
from app.main import create_app
from app.settings import Settings
from app.supply import contract_expiry
from app.supply.contract_expiry_mail import Mailer
from tests.integration.conftest import MIGRATIONS
from tests.integration.supply import purge_supply
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ADMIN, MAKER, SALES, GONE, NO_MAIL = (
    "it-1177-admin",
    "it-1177-maker",
    "it-1177-sales",
    "it-1177-gone",
    "it-1177-nomail",
)
STAFF = [
    (ADMIN, "admin", True, "lead@example.test"),
    (MAKER, "contracting", True, "rates@example.test"),
    (SALES, "sales", True, "desk@example.test"),  # not a mail role
    (GONE, "admin", False, "former@example.test"),  # inactive
    (NO_MAIL, "contracting", True, None),  # no address
]
POLICY = [p for p in MIGRATIONS if p.name[:3] in ("004", "006", "007")]
TEST_POLICY_MAKER = "it-1177-policy"


async def _clean(conn: asyncpg.Connection) -> None:
    await conn.execute("DELETE FROM booking.notifications WHERE type = $1", contract_expiry.NOTIFICATION_TYPE)
    await conn.execute(
        "DELETE FROM booking.pricing_policy_events WHERE policy_id IN"
        " (SELECT id FROM booking.pricing_policy WHERE created_by = $1)",
        TEST_POLICY_MAKER,
    )
    await conn.execute("DELETE FROM booking.pricing_policy WHERE created_by = $1", TEST_POLICY_MAKER)
    await purge_supply(conn)
    await conn.execute("DELETE FROM booking.supplier_contracts")
    await conn.execute("DELETE FROM booking.suppliers")


async def _staff(conn: asyncpg.Connection) -> None:
    """This test's staff rows (again after HTTP calls: a sign-in refreshes the caller's row from the token)."""
    for staff_id, role, active, email in STAFF:
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active, name, email) VALUES ($1, $2, $3, $1, $4)"
            " ON CONFLICT (cis_user_id) DO UPDATE SET role = EXCLUDED.role, active = EXCLUDED.active,"
            " email = EXCLUDED.email",
            staff_id,
            role,
            active,
            email,
        )


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    for path in POLICY:
        await conn.execute(path.read_text(encoding="utf-8"))
    await _clean(conn)
    # Only this test's staff hold a mail role while it runs (other modules leave their staff rows behind); the rows
    # it switches off are switched on again at the end.
    others = await conn.fetch(
        "UPDATE booking.staff_users SET active = false WHERE active AND cis_user_id NOT LIKE 'it-1177-%'"
        " RETURNING cis_user_id"
    )
    await _staff(conn)
    try:
        yield conn
    finally:
        await _clean(conn)
        await conn.execute(
            "UPDATE booking.staff_users SET active = true WHERE cis_user_id = ANY($1::text[])",
            [r["cis_user_id"] for r in others],
        )
        await conn.close()


@pytest_asyncio.fixture
async def client(
    admin: asyncpg.Connection, migrated_booking_dsn: str
) -> AsyncIterator[tuple[httpx.AsyncClient, LocalCis]]:
    cis = LocalCis()
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db = Database(settings)
    app = create_app(settings, db, cis.verifier())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            yield c, cis
    finally:
        await db.close()


@pytest_asyncio.fixture
async def pool(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


def _as(cis: LocalCis, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role='reviewer')}"}


async def _ok(response: httpx.Response) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


async def _contract(admin: asyncpg.Connection, name: str, category: str, today: date, days: int) -> tuple[str, str]:
    """A published contract of an invented supplier of ``category`` ending ``days`` from today: (contract, code)."""
    supplier, code = await admin.fetchrow(
        "INSERT INTO booking.suppliers (company_name, categories, country_code, status, created_by)"
        " VALUES ($1, ARRAY[$2], 'VN', 'active', $3) RETURNING id::text, code",
        name,
        category,
        MAKER,
    )
    k = await admin.fetchval(
        "INSERT INTO booking.supplier_contracts (supplier_id, title, valid_from, valid_to, currency, created_by)"
        " VALUES ($1::uuid, $2, $3, $4, 'USD', $5) RETURNING id::text",
        supplier,
        f"{name} rates",
        today - timedelta(days=300),
        today + timedelta(days=days),
        MAKER,
    )
    await admin.execute(
        "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2, published_at = now()"
        " WHERE id = $1::uuid",
        k,
        ADMIN,
    )
    return k, code


async def _window(admin: asyncpg.Connection, days: int, today: date) -> None:
    """A new approved version of contract_expiry_alert_days in force from today."""
    last = await admin.fetchrow(
        "SELECT id, version_no FROM booking.pricing_policy WHERE param_key = 'contract_expiry_alert_days'"
        " ORDER BY version_no DESC LIMIT 1"
    )
    pid = await admin.fetchval(
        "INSERT INTO booking.pricing_policy (param_key, version_no, value, unit, effective_from, reason, replaces_id,"
        " created_by) VALUES ('contract_expiry_alert_days', $1, $2::jsonb, 'days', $3, 'invented window check', $4, $5)"
        " RETURNING id",
        last["version_no"] + 1,
        json.dumps(days),
        today,
        last["id"],
        TEST_POLICY_MAKER,
    )
    await admin.execute(
        "INSERT INTO booking.pricing_policy_events (policy_id, maker, event, actor) VALUES"
        " ($1, $2, 'submitted', $2), ($1, $2, 'approved', $3)",
        pid,
        TEST_POLICY_MAKER,
        ADMIN,
    )


class FakeSes:
    def __init__(self) -> None:
        self.sent: list[mailer.Message] = []

    async def __call__(self, message: mailer.Message) -> str:
        self.sent.append(message)
        return f"msg-{len(self.sent)}"


async def test_done_when_hotel_contract_listed_announced_and_mailed_once(
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection, pool: asyncpg.Pool
) -> None:
    c, cis = client
    today = await admin.fetchval("SELECT (now() AT TIME ZONE 'UTC')::date")
    hotel, hotel_code = await _contract(admin, "Riverside Garden Hotel", "hotel", today, 20)
    car, _ = await _contract(admin, "Lotus Road Cars", "transport", today, 50)
    await _contract(admin, "Bamboo Trail Guides", "guide", today, 90)  # outside the 60-day window

    # Every kind is listed before the first run; one supplier's list; sales reads it.
    listed = await _ok(await c.get("/contracts/expiring", headers=_as(cis, SALES)))
    assert [(i["id"], i["supplier_categories"], i["announced"], i["mailed"]) for i in listed["items"]] == [
        (hotel, ["hotel"], False, False),
        (car, ["transport"], False, False),
    ]
    one = await _ok(await c.get(f"/contracts/expiring?supplier={hotel_code}", headers=_as(cis, SALES)))
    assert [i["id"] for i in one["items"]] == [hotel]

    # The My work tile of the people who renew contracts (supply.write); sales has none.
    tiles = {t["key"]: t for t in (await _ok(await c.get("/my-work/summary", headers=_as(cis, MAKER))))["tiles"]}
    tile = tiles["contracts_ending_soon"]
    assert (tile["count"], tile["days"], tile["earliest_due"], tile["filter"]) == (
        2,
        60,
        (today + timedelta(days=20)).isoformat(),
        {"contracts": "ending"},
    )
    sales_tiles = (await _ok(await c.get("/my-work/summary", headers=_as(cis, SALES))))["tiles"]
    assert "contracts_ending_soon" not in {t["key"] for t in sales_tiles}

    # The job: one notification and one e-mail per active admin / contracting member with an address.
    await _staff(admin)
    ses = FakeSes()
    mail = Mailer(ses, "test", "https://booking.example.test")
    job = Job(id="j-1177", kind=contract_expiry.KIND, payload={"day": today.isoformat()}, attempt=1, max_attempts=3)
    first = await contract_expiry.handle_watch(pool, job, mail)
    assert first["announced_count"] == 2 and first["mail"]["sent"] == 4 and first["mail"]["contracts_done"] == 2
    assert sorted({m.to for m in ses.sent}) == ["lead@example.test", "rates@example.test"]
    hotel_mail = next(m for m in ses.sent if "Riverside Garden Hotel" in m.subject)
    assert f"/suppliers/{hotel_code}?tab=contracts" in hotel_mail.text and "Khách sạn" in hotel_mail.text
    record = json.loads(
        await admin.fetchval(
            "SELECT data -> 'mail' FROM booking.notifications"
            " WHERE type = 'contract.expiring' AND entity_id = $1::uuid",
            hotel,
        )
    )
    assert sorted(record["sent"]) == [ADMIN, MAKER] and record["done"] is True and record["refused"] == []

    # A second run sends nothing and announces nothing.
    again = await contract_expiry.handle_watch(pool, job, mail)
    assert again["announced_count"] == 0 and again["mail"]["sent"] == 0 and len(ses.sent) == 4
    assert await admin.fetchval("SELECT count(*) FROM booking.notifications WHERE type = 'contract.expiring'") == 2
    listed = await _ok(await c.get("/contracts/expiring", headers=_as(cis, SALES)))
    assert all(i["announced"] and i["mailed"] for i in listed["items"])

    # The window is the policy value: 30 days lists the hotel contract only.
    await _window(admin, 30, today)
    narrowed = await _ok(await c.get("/contracts/expiring", headers=_as(cis, SALES)))
    assert narrowed["window_days"] == 30 and [i["id"] for i in narrowed["items"]] == [hotel]
    tiles = {t["key"]: t for t in (await _ok(await c.get("/my-work/summary", headers=_as(cis, ADMIN))))["tiles"]}
    assert tiles["contracts_ending_soon"]["count"] == 1 and tiles["contracts_ending_soon"]["days"] == 30


async def test_roles_off_or_without_staff_mail_nobody_and_mail_later(
    admin: asyncpg.Connection, pool: asyncpg.Pool
) -> None:
    today = await admin.fetchval("SELECT (now() AT TIME ZONE 'UTC')::date")
    await _contract(admin, "Silk Lantern Hotel", "hotel", today, 10)
    ses = FakeSes()
    mail = Mailer(ses, "test", "https://booking.example.test")
    job = Job(id="j-1177b", kind=contract_expiry.KIND, payload={"day": today.isoformat()}, attempt=1, max_attempts=3)
    await admin.execute("UPDATE booking.staff_users SET active = false WHERE cis_user_id LIKE 'it-1177-%'")
    first = await contract_expiry.handle_watch(pool, job, mail)
    assert first["announced_count"] == 1 and first["mail"]["outcome"] == "no_recipients" and ses.sent == []
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM booking.notifications WHERE type = 'contract.expiring' AND data ? 'mail'"
        )
        == 0
    )
    # Someone takes the role: the next run mails the contract still in the window.
    await admin.execute("UPDATE booking.staff_users SET active = true WHERE cis_user_id = $1", MAKER)
    later = await contract_expiry.handle_watch(pool, job, mail)
    assert (
        later["announced_count"] == 0
        and later["mail"]["sent"] == 1
        and [m.to for m in ses.sent] == ["rates@example.test"]
    )
