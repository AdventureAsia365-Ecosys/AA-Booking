"""AA-1091 (S280, root ADR 0006a A10e-3): the synthetic-supply SQL of the Dev-only pricing pair
(:mod:`app.auth.e2e_staff`) on real PostgreSQL, as role ``booking``.

Covers: a supplier the E2E pricing maker created with the marked name answers True, and so do its hotel, service and
contract (by UUID and by code, and the supplier a hotel / service create body names); a supplier another person
created, one the maker created but someone renamed, one with no creator, and an unknown id answer
False, with their records.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.auth import e2e_staff

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MAKER_KEY = "it-1091s-maker"
OTHER_KEY = "it-1091s-other"
NAMES = {
    "maker": "SYNTHETIC it-1091s maker",
    "other": "SYNTHETIC it-1091s other",
    "renamed": "it-1091s renamed",
    "orphan": "SYNTHETIC it-1091s orphan",
}
# created_by is a staff_users FK: the "orphan" supplier has no creator at all (a row made outside the API).
CREATOR: dict[str, str | None] = {"maker": MAKER_KEY, "other": OTHER_KEY, "renamed": MAKER_KEY, "orphan": None}


async def _purge(conn: asyncpg.Connection) -> None:
    ours = "(SELECT id FROM booking.suppliers WHERE company_name LIKE '%it-1091s %')"
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE bk_owner")
        await conn.execute(f"DELETE FROM booking.supplier_contracts WHERE supplier_id IN {ours}")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.hotels WHERE supplier_id IN {ours}")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.services WHERE supplier_id IN {ours}")  # noqa: S608
        await conn.execute("DELETE FROM booking.suppliers WHERE company_name LIKE '%it-1091s %'")
        await conn.execute(
            "DELETE FROM booking.staff_users WHERE cis_user_id = ANY($1::text[])", [MAKER_KEY, OTHER_KEY]
        )


async def _seed(conn: asyncpg.Connection) -> dict[str, dict[str, Any]]:
    """Per case a supplier with one hotel, one service and one draft contract."""
    out: dict[str, dict[str, Any]] = {}
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE bk_owner")
        for key, email in ((MAKER_KEY, e2e_staff.E2E_PRICING_MAKER_EMAIL), (OTHER_KEY, "lena@adventure.asia")):
            await conn.execute(
                "INSERT INTO booking.staff_users (cis_user_id, role, active, name, email)"
                " VALUES ($1, 'contracting', true, $1, $2)",
                key,
                email,
            )
        for case, name in NAMES.items():
            supplier = await conn.fetchrow(
                "INSERT INTO booking.suppliers (company_name, categories, country_code, created_by, updated_by)"
                " VALUES ($1, ARRAY['hotel','activity'], 'JP', $2, $2) RETURNING id::text, code",
                "SYNTHETIC it-1091s renamed-from" if case == "renamed" else name,
                CREATOR[case],
            )
            if case == "renamed":  # a real person renamed it afterwards: no longer synthetic
                await conn.execute(
                    "UPDATE booking.suppliers SET company_name = $2, updated_by = $3 WHERE id = $1::uuid",
                    supplier["id"],
                    name,
                    OTHER_KEY,
                )
            hotel = await conn.fetchrow(
                "INSERT INTO booking.hotels (supplier_id, name) VALUES ($1::uuid, 'SYNTHETIC hotel')"
                " RETURNING id::text, code",
                supplier["id"],
            )
            service = await conn.fetchrow(
                "INSERT INTO booking.services (supplier_id, service_type, title, unit)"
                " VALUES ($1::uuid, 'activity', 'SYNTHETIC activity', 'person') RETURNING id::text, code",
                supplier["id"],
            )
            contract = await conn.fetchval(
                "INSERT INTO booking.supplier_contracts (supplier_id, title, valid_from, currency, created_by)"
                " VALUES ($1::uuid, 'SYNTHETIC rates', $2, 'USD', $3) RETURNING id::text",
                supplier["id"],
                date(2031, 1, 1),
                CREATOR[case] or OTHER_KEY,
            )
            out[case] = {
                "supplier": dict(supplier),
                "hotel": dict(hotel),
                "service": dict(service),
                "contract": contract,
            }
    return out


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
    try:
        yield conn
    finally:
        await _purge(conn)
        await conn.close()


@pytest_asyncio.fixture
async def booking(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _answers(booking: asyncpg.Connection, rows: dict[str, Any]) -> dict[str, bool]:
    s, h, v, k = rows["supplier"], rows["hotel"], rows["service"], rows["contract"]
    checks = {
        "supplier_id": (e2e_staff.SUPPLIER_DEACTIVATE[1], {"supplier_ref": s["id"]}, None),
        "supplier_code": (e2e_staff.RATE_SHEET_IMPORT[1], {"supplier_ref": s["code"].lower()}, None),
        "hotel_id": (e2e_staff.ROOM_TYPE_CREATE[1], {"hotel_ref": h["id"]}, None),
        "hotel_code": (e2e_staff.HOTEL_DEACTIVATE[1], {"hotel_ref": h["code"]}, None),
        "service_code": (e2e_staff.SERVICE_DEACTIVATE[1], {"service_ref": v["code"]}, None),
        "contract": (e2e_staff.CONTRACT_STATUS[1], {"contract_id": k}, None),
        "hotel_create": (e2e_staff.HOTEL_CREATE[1], {}, {"supplier_id": s["code"], "name": "SYNTHETIC x"}),
        "service_create": (e2e_staff.SERVICE_CREATE[1], {}, {"supplier_id": s["id"], "title": "SYNTHETIC x"}),
    }
    return {
        name: await e2e_staff.is_synthetic_record(booking, route, params, body)
        for name, (route, params, body) in checks.items()
    }


async def test_only_the_makers_marked_supplier_and_its_records_answer_true(
    admin: asyncpg.Connection, booking: asyncpg.Connection
) -> None:
    rows = await _seed(admin)
    assert all((await _answers(booking, rows["maker"])).values())
    for case in ("other", "renamed", "orphan"):
        assert not any((await _answers(booking, rows[case])).values()), case
    unknown = str(uuid.uuid4())
    assert not await e2e_staff.is_synthetic_record(booking, e2e_staff.CONTRACT_STATUS[1], {"contract_id": unknown})
    assert not await e2e_staff.is_synthetic_record(
        booking, e2e_staff.SUPPLIER_DEACTIVATE[1], {"supplier_ref": "SUP-999999999"}
    )
