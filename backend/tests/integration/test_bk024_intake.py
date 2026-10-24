"""AA-908: inquiry.submitted v1.4 (signed-in traveller) through the real intake jobs on PostgreSQL, as role ``booking``.

The verified identity becomes one verified customer (booking.customer_identities, bk-024); the same identity again
reuses it; an identity whose email belongs to an existing verified customer attaches to it; unverified leads with the
same email are flagged for merge review and never merged; a v1.4 guest send and a v1.3 send behave as before.
Fixtures and helpers: test_bk002_intake.py."""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from tests import inquiry_samples as samples
from tests.integration.test_bk002_intake import admin, drain, pool, publish  # noqa: F401 (fixtures)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _customer_of_case(admin: asyncpg.Connection, payload: dict) -> asyncpg.Record:  # noqa: F811
    return await admin.fetchrow(
        "SELECT c.* FROM booking.trip_cases t JOIN booking.customers c ON c.id = t.customer_id"
        " WHERE t.idempotency_key = $1",
        payload["idempotency_key"],
    )


async def test_new_identity_creates_a_verified_customer_and_reuses_it(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
) -> None:
    email, sub = _email(), str(uuid.uuid4())
    first = samples.v1_4(email, sub=sub)
    first["customer"]["phone"] = "+84900000099"  # typed by the traveller: never imported into a verified customer
    await publish(admin, first)
    await drain(pool)
    customer = await _customer_of_case(admin, first)
    assert (customer["verified"], customer["source"], customer["email"], customer["phone"]) == (
        True,
        "traveller_signin",
        email,
        None,
    )
    assert customer["needs_merge_review"] is False
    rows = await admin.fetch("SELECT iss, sub, customer_id FROM booking.customer_identities")
    assert [tuple(r) for r in rows] == [(samples.ISS, sub, customer["id"])]

    second = samples.v1_4(email, sub=sub)
    await publish(admin, second)
    await drain(pool)
    assert (await _customer_of_case(admin, second))["id"] == customer["id"]
    assert await admin.fetchval("SELECT count(*) FROM booking.customers") == 1
    assert await admin.fetchval("SELECT count(*) FROM booking.customer_identities") == 1
    statuses = [r["status"] for r in await admin.fetch("SELECT status FROM booking.inbox_events")]
    assert statuses == ["processed", "processed"]


async def test_identity_attaches_to_the_verified_customer_with_that_email(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
) -> None:
    email = _email()
    existing = await admin.fetchval(
        "INSERT INTO booking.customers (name, email, verified, source) VALUES ('Known', $1, true, 'otp') RETURNING id",
        email,
    )
    payload = samples.v1_4(email.upper())
    await publish(admin, payload)
    await drain(pool)
    assert (await _customer_of_case(admin, payload))["id"] == existing
    assert await admin.fetchval("SELECT customer_id FROM booking.customer_identities") == existing
    assert await admin.fetchval("SELECT count(*) FROM booking.customers") == 1


async def test_unverified_leads_with_the_same_email_are_flagged_never_merged(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
) -> None:
    email = _email()
    guest = samples.v1_3(email)  # an earlier guest send: an unverified lead
    await publish(admin, guest)
    await drain(pool)
    lead = await _customer_of_case(admin, guest)
    assert (lead["verified"], lead["source"], lead["needs_merge_review"]) == (False, "tripplanner_lead", False)

    signed = samples.v1_4(email)
    await publish(admin, signed)
    await drain(pool)
    verified = await _customer_of_case(admin, signed)
    assert verified["id"] != lead["id"] and verified["verified"] is True
    lead_after = await admin.fetchrow("SELECT * FROM booking.customers WHERE id = $1", lead["id"])
    assert (lead_after["needs_merge_review"], lead_after["merge_candidate_id"], lead_after["deleted_at"]) == (
        True,
        verified["id"],
        None,
    )
    # the guest's trip case stays on the lead: nothing is moved
    assert (await _customer_of_case(admin, guest))["id"] == lead["id"]
    note = await admin.fetchval(
        "SELECT message FROM booking.notifications WHERE data->>'customer_id' = $1", str(verified["id"])
    )
    assert "Signed-in traveller" in note and "1 earlier unverified lead(s)" in note


async def test_archived_customer_identity_is_repointed(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
) -> None:
    email, sub = _email(), str(uuid.uuid4())
    first = samples.v1_4(email, sub=sub)
    await publish(admin, first)
    await drain(pool)
    old = (await _customer_of_case(admin, first))["id"]
    await admin.execute("UPDATE booking.customers SET deleted_at = now() WHERE id = $1", old)

    again = samples.v1_4(email, sub=sub)
    await publish(admin, again)
    await drain(pool)
    new = (await _customer_of_case(admin, again))["id"]
    assert new != old
    assert await admin.fetchval("SELECT customer_id FROM booking.customer_identities WHERE sub = $1", sub) == new


async def test_guest_v1_4_and_v1_3_sends_stay_leads(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
) -> None:
    for payload in (samples.v1_4(signed_in=False), samples.v1_3()):
        await publish(admin, payload)
        await drain(pool)
        customer = await _customer_of_case(admin, payload)
        assert (customer["verified"], customer["source"]) == (False, "tripplanner_lead")
    assert await admin.fetchval("SELECT count(*) FROM booking.customer_identities") == 0
