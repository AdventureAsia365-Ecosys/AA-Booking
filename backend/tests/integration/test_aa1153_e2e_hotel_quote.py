"""AA-1153 (S289, root ADR 0006a A10e-5): the record checks of the pricing maker's hotel link and desk entry
(:mod:`app.auth.e2e_staff_supply`) on real PostgreSQL, as role ``booking``.

``catalog.v_tenant_account_v1`` (CIS 357 + 401) is stood in by a plain table around each test. Covers: a link of the
maker's synthetic hotel to its second synthetic DMC answers True; a real DMC, or a real hotel, answers False. A desk
entry answers True only for a synthetic agency and room types on a synthetic hotel all of whose DMCs are synthetic: a
real agency, a hotel also linked to a real DMC (even inactive) or an unknown room type answer False. All names invented.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.auth import e2e_staff

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CIS_401_COLUMNS = (
    "tenant_id uuid, name text, account_status text, suspend_reason text, in_service boolean, kind text, "
    "is_pilot boolean, trade_book boolean, is_synthetic boolean"
)
DROP = "DROP TABLE IF EXISTS catalog.v_tenant_account_v1"
SYNTHETIC = uuid.UUID("00000000-0000-4000-8000-0000000a1153")
REAL = uuid.UUID("00000000-0000-4000-8000-0000000b1153")
MAKER_KEY, OTHER_KEY = "it-1153e-maker", "it-1153e-other"
TAG = "it-1153e"
LINK = e2e_staff.HOTEL_SUPPLIER_LINK[1]
DESK = e2e_staff.DESK_ENTRY[1]


async def _purge(conn: asyncpg.Connection) -> None:
    ours = f"(SELECT id FROM booking.suppliers WHERE company_name LIKE '%{TAG} %')"
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE bk_owner")
        hotels = f"(SELECT id FROM booking.hotels WHERE supplier_id IN {ours})"  # noqa: S608
        await conn.execute(f"DELETE FROM booking.hotel_supplier_links WHERE hotel_id IN {hotels}")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.hotel_room_types WHERE hotel_id IN {hotels}")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.hotels WHERE supplier_id IN {ours}")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.suppliers WHERE company_name LIKE '%{TAG} %'")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.b2b_partners WHERE agency_name LIKE '{TAG} %'")  # noqa: S608
        await conn.execute(
            "DELETE FROM booking.staff_users WHERE cis_user_id = ANY($1::text[])", [MAKER_KEY, OTHER_KEY]
        )
    await conn.execute(DROP)


async def _hotel(conn: asyncpg.Connection, supplier: str, name: str) -> dict[str, str]:
    hotel = await conn.fetchval(
        "INSERT INTO booking.hotels (supplier_id, name) VALUES ($1::uuid, $2) RETURNING id::text", supplier, name
    )
    room = await conn.fetchval(
        "INSERT INTO booking.hotel_room_types (hotel_id, name) VALUES ($1::uuid, 'Double') RETURNING id::text", hotel
    )
    return {"hotel": hotel, "room": room}


async def _link(conn: asyncpg.Connection, hotel: str, supplier: str, status: str = "active") -> None:
    await conn.execute(
        "INSERT INTO booking.hotel_supplier_links (hotel_id, supplier_id, status, created_by)"
        " VALUES ($1::uuid, $2::uuid, $3, $4)",
        hotel,
        supplier,
        status,
        MAKER_KEY,
    )


async def _seed(conn: asyncpg.Connection) -> dict[str, Any]:
    await conn.execute(DROP)
    await conn.execute(f"CREATE TABLE catalog.v_tenant_account_v1 ({CIS_401_COLUMNS})")
    await conn.execute("GRANT SELECT ON catalog.v_tenant_account_v1 TO booking")
    for tid, synthetic in ((SYNTHETIC, True), (REAL, False)):
        await conn.execute(
            "INSERT INTO catalog.v_tenant_account_v1 VALUES ($1, 'Fernhollow Trips', 'active', NULL, true,"
            " 'agency', false, true, $2)",
            tid,
            synthetic,
        )
    out: dict[str, Any] = {}
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE bk_owner")
        for key, email in ((MAKER_KEY, e2e_staff.E2E_PRICING_MAKER_EMAIL), (OTHER_KEY, "lena@adventure.asia")):
            await conn.execute(
                "INSERT INTO booking.staff_users (cis_user_id, role, active, name, email)"
                " VALUES ($1, 'contracting', true, $1, $2)",
                key,
                email,
            )
        for case, name, creator in (
            ("first", f"SYNTHETIC {TAG} first DMC", MAKER_KEY),
            ("second", f"SYNTHETIC {TAG} second DMC", MAKER_KEY),
            ("real", f"{TAG} Lantern DMC", OTHER_KEY),
        ):
            out[case] = await conn.fetchval(
                "INSERT INTO booking.suppliers (company_name, categories, country_code, created_by, updated_by)"
                " VALUES ($1, ARRAY['hotel'], 'TH', $2, $2) RETURNING id::text",
                name,
                creator,
            )
        out["lodge"] = await _hotel(conn, out["first"], "SYNTHETIC lodge")  # linked to its first DMC by trigger
        await _link(conn, out["lodge"]["hotel"], out["second"])
        out["mixed"] = await _hotel(conn, out["first"], "SYNTHETIC mixed")
        await _link(conn, out["mixed"]["hotel"], out["real"], "inactive")  # a real DMC, even inactive
        out["real_hotel"] = await _hotel(conn, out["real"], "Harbour View Inn")
        for label, tid in (("synthetic", SYNTHETIC), ("real", REAL)):
            out[f"partner_{label}"] = await conn.fetchval(
                "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
                " VALUES ($1, $2, 'active', 'USD', 'aa1153eclient') RETURNING id::text",
                tid,
                f"{TAG} {label}",
            )
    return out


@pytest_asyncio.fixture
async def seeded(migrated_admin_dsn: str) -> AsyncIterator[dict[str, Any]]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
    try:
        yield await _seed(conn)
    finally:
        await _purge(conn)
        await conn.close()


@pytest_asyncio.fixture
async def booking(seeded: dict[str, Any], migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_a_link_needs_both_ends_synthetic(booking: asyncpg.Connection, seeded: dict[str, Any]) -> None:
    lodge, real_hotel = seeded["lodge"]["hotel"], seeded["real_hotel"]["hotel"]
    check = e2e_staff.is_synthetic_record
    assert await check(booking, LINK, {"hotel_ref": lodge}, {"supplier_id": seeded["second"]}) is True
    assert await check(booking, LINK, {"hotel_ref": lodge}, {"supplier_id": seeded["real"]}) is False
    assert await check(booking, LINK, {"hotel_ref": real_hotel}, {"supplier_id": seeded["second"]}) is False
    assert await check(booking, LINK, {"hotel_ref": str(uuid.uuid4())}, {"supplier_id": seeded["second"]}) is False


def _desk(partner: str, *rooms: str) -> dict[str, Any]:
    lines = [{"line_ref": f"l{i}", "component_id": f"rt:{r}", "pax": {"adult": 2}} for i, r in enumerate(rooms)]
    return {"partner_id": partner, "agency_ref": "SYNTHETIC check", "currency": "USD", "lines": lines}


async def test_a_desk_entry_needs_a_synthetic_agency_and_synthetic_rooms(
    booking: asyncpg.Connection, seeded: dict[str, Any]
) -> None:
    synthetic, real = seeded["partner_synthetic"], seeded["partner_real"]
    lodge, mixed, real_room = seeded["lodge"]["room"], seeded["mixed"]["room"], seeded["real_hotel"]["room"]
    answers = {
        "synthetic": await e2e_staff.is_synthetic_record(booking, DESK, {}, _desk(synthetic, lodge, lodge)),
        "real_agency": await e2e_staff.is_synthetic_record(booking, DESK, {}, _desk(real, lodge)),
        "real_dmc_link": await e2e_staff.is_synthetic_record(booking, DESK, {}, _desk(synthetic, lodge, mixed)),
        "real_hotel": await e2e_staff.is_synthetic_record(booking, DESK, {}, _desk(synthetic, real_room)),
        "unknown_room": await e2e_staff.is_synthetic_record(booking, DESK, {}, _desk(synthetic, str(uuid.uuid4()))),
        "unknown_agency": await e2e_staff.is_synthetic_record(booking, DESK, {}, _desk(str(uuid.uuid4()), lodge)),
    }
    assert answers == {
        "synthetic": True,
        "real_agency": False,
        "real_dmc_link": False,
        "real_hotel": False,
        "unknown_room": False,
        "unknown_agency": False,
    }
