"""AA-996 (bk-031) on a real PostgreSQL: the CIS tour countries Booking's reference list was missing (Kyrgyzstan,
Pakistan, Taiwan, Uzbekistan) are there with CIS's names and active, the file registers itself and re-runs as a no-op,
an existing row is never overwritten, and role ``booking`` still only reads the list (the query of
``GET /booking/countries`` sees the new rows)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "031_countries_cis_markets.sql"
ADDED = {"KG": "Kyrgyzstan", "PK": "Pakistan", "TW": "Taiwan", "UZ": "Uzbekistan"}
# CIS migration 279 checks that each AA tour country resolves (chk_raw_tours_country): all of them are in Booking's
# reference list after bk-031, under the same name, so the name matches of app/catalog/* find them.
AA_TOUR_COUNTRIES = (
    "Bhutan", "Cambodia", "China", "India", "Indonesia", "Japan", "Kyrgyzstan", "Laos", "Malaysia", "Mongolia",
    "Myanmar", "Nepal", "Pakistan", "Philippines", "South Korea", "Sri Lanka", "Taiwan", "Thailand", "Uzbekistan",
    "Vietnam",
)  # fmt: skip


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_registered_and_rerunnable(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))  # a second run is a no-op
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-031'") == 1
    rows = await admin.fetch(
        "SELECT iso2::text AS iso2, name, active FROM booking.countries WHERE iso2 = ANY($1)", list(ADDED)
    )
    assert {r["iso2"]: (r["name"], r["active"]) for r in rows} == {k: (v, True) for k, v in ADDED.items()}


async def test_existing_rows_are_never_overwritten(admin: asyncpg.Connection) -> None:
    """ON CONFLICT DO NOTHING: a row an earlier file (or a later decision) set keeps its name and active flag."""
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute("UPDATE booking.countries SET active = false WHERE iso2 = 'UZ'")
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
        assert await admin.fetchval("SELECT active FROM booking.countries WHERE iso2 = 'UZ'") is False
        assert await admin.fetchval("SELECT name FROM booking.countries WHERE iso2 = 'VN'") == "Vietnam"
    finally:
        await admin.execute("UPDATE booking.countries SET active = true WHERE iso2 = 'UZ'")
        await admin.execute("RESET ROLE")


async def test_every_aa_tour_country_is_in_the_reference_list(app_conn: asyncpg.Connection) -> None:
    names = {r["name"] for r in await app_conn.fetch("SELECT name FROM booking.countries WHERE active")}
    assert set(AA_TOUR_COUNTRIES) <= names, sorted(set(AA_TOUR_COUNTRIES) - names)


async def test_names_match_the_cis_country_view(admin: asyncpg.Connection) -> None:
    """Booking maps CIS country names by name: the added rows carry catalog.v_country's name (CI stub of CIS 279)."""
    rows = await admin.fetch(
        "SELECT b.iso2::text AS iso2, b.name AS booking_name, c.name AS cis_name"
        " FROM booking.countries b JOIN catalog.v_country c ON c.iso2 = b.iso2 WHERE b.iso2 = ANY($1)",
        list(ADDED),
    )
    assert rows and all(r["booking_name"] == r["cis_name"] for r in rows), [dict(r) for r in rows]


async def test_role_booking_still_only_reads(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert not await admin.fetchval("SELECT has_table_privilege('booking', 'booking.countries', $1)", privilege)
    assert await admin.fetchval("SELECT has_table_privilege('booking', 'booking.countries', 'SELECT')")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("INSERT INTO booking.countries (iso2, name) VALUES ('KZ', 'Kazakhstan')")
    listed = {r["iso2"] for r in await app_conn.fetch("SELECT iso2::text AS iso2 FROM booking.countries WHERE active")}
    assert set(ADDED) <= listed
