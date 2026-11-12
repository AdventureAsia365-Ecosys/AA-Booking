"""AA-1087 (S278, root ADR 0006a A10e): the synthetic-record SQL of the Dev-only E2E write identities
(:mod:`app.auth.e2e_staff`) on real PostgreSQL, as role ``booking``.

``catalog.v_tenant_account_v1`` (CIS 357 + 401) is stood in by a plain table of 401's exact columns around each test
(as ``test_aa969_pilot_skip``). Rows are seeded as ``bk_owner`` with the row guards off for the seed only. Covers:

- a supplier booking of a synthetic agency's B2B case, an agency invoice (by id and by number) and an agency payment
  of a synthetic agency answer True; the same records of a real agency, of no agency (a departure-owned supplier
  booking) and unknown ids answer False;
- without the ``is_synthetic`` column (CIS 401 not applied: 357's columns only) every record answers False (fail
  closed), and the request path then refuses the write.
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
from tests.integration import ops

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CIS_357_COLUMNS = (
    "tenant_id uuid, name text, account_status text, suspend_reason text, in_service boolean, kind text, "
    "is_pilot boolean, trade_book boolean"
)
CIS_401_COLUMNS = CIS_357_COLUMNS + ", is_synthetic boolean"
DROP = "DROP TABLE IF EXISTS catalog.v_tenant_account_v1"
SYNTHETIC = uuid.UUID("00000000-0000-4000-8000-0000000a1087")
REAL = uuid.UUID("00000000-0000-4000-8000-0000000b1087")
NOTE = "it-1087"
GUARDED = ("booking.supplier_bookings", "booking.agency_payment", "booking.agency_invoice")


async def _stub(conn: asyncpg.Connection, columns: str) -> None:
    await conn.execute(DROP)
    await conn.execute(f"CREATE TABLE catalog.v_tenant_account_v1 ({columns})")
    await conn.execute("GRANT SELECT ON catalog.v_tenant_account_v1 TO booking")
    if "is_synthetic" in columns:
        for tid, synthetic in ((SYNTHETIC, True), (REAL, False)):
            await conn.execute(
                "INSERT INTO catalog.v_tenant_account_v1 VALUES ($1, 'Fernhollow Trips', 'active', NULL, true,"
                " 'agency', false, true, $2)",
                tid,
                synthetic,
            )


async def _purge(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE bk_owner")
        for t in GUARDED:
            await conn.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
        await conn.execute(
            "DELETE FROM booking.agency_payment WHERE invoice_id IN"
            " (SELECT id FROM booking.agency_invoice WHERE created_by = $1)",
            NOTE,
        )
        await conn.execute("DELETE FROM booking.agency_invoice WHERE created_by = $1", NOTE)
        await conn.execute("DELETE FROM booking.supplier_bookings WHERE created_by = $1", NOTE)
        for t in GUARDED:
            await conn.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")
        await conn.execute("DELETE FROM booking.trip_case_status_history WHERE trip_case_id IN"
                           " (SELECT id FROM booking.trip_cases WHERE notes = $1)", NOTE)  # fmt: skip
        await conn.execute("DELETE FROM booking.trip_cases WHERE notes = $1", NOTE)
        await conn.execute("DELETE FROM booking.suppliers WHERE company_name = 'it-1087 Ridgeway Lodges'")
        await conn.execute("DELETE FROM booking.b2b_partners WHERE agency_name LIKE 'it-1087 %'")
        await conn.execute("DELETE FROM booking.billing_entity WHERE code = 'IT1087AA'")
    await conn.execute(DROP)


async def _seed(conn: asyncpg.Connection) -> dict[str, Any]:
    """Per agency: a B2B case with one supplier booking, an issued invoice and one recorded payment; plus one
    departure-owned supplier booking (no agency)."""
    out: dict[str, Any] = {}
    await ops.staff(conn, (NOTE, "sales"))  # created_by / recorded_by name a staff row
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE bk_owner")
        for t in GUARDED:
            await conn.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
        supplier = await conn.fetchval(
            "INSERT INTO booking.suppliers (company_name, categories, country_code)"
            " VALUES ('it-1087 Ridgeway Lodges', ARRAY['hotel'], 'VN') RETURNING id"
        )
        entity = await conn.fetchval(
            "INSERT INTO booking.billing_entity (code, legal_entity) VALUES ('IT1087AA', 'Lanternfield Travel Ltd')"
            " RETURNING id"
        )
        for label, tid in (("synthetic", SYNTHETIC), ("real", REAL)):
            await conn.execute(
                "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
                " VALUES ($1, $2, 'active', 'USD', 'aa1087client')",
                tid,
                f"{NOTE} {label}",
            )
            case = await conn.fetchval(
                "INSERT INTO booking.trip_cases (channel, party_tenant_id, status, start_date, end_date, pax, currency,"
                " source_system, notes) VALUES ('b2b', $1, 'confirmed', $2, $3, 2, 'USD', 'crm', $4) RETURNING id",
                tid,
                date(2027, 3, 1),
                date(2027, 3, 5),
                NOTE,
            )
            booking = await conn.fetchval(
                "INSERT INTO booking.supplier_bookings (trip_case_id, supplier_id, item_description, status,"
                " requested_at, created_by) VALUES ($1, $2, 'Two nights, garden room', 'requested', now(), $3)"
                " RETURNING id",
                case,
                supplier,
                NOTE,
            )
            invoice = await conn.fetchrow(
                "INSERT INTO booking.agency_invoice (party_tenant_id, invoice_kind, status, period_start, period_end,"
                " currency, subtotal, tax_rate, tax_amount, total, source, created_by, idempotency_key, number,"
                " issued_at, issued_by, issued_on, due_date, issuer, bill_to, billing_entity_id)"
                " VALUES ($1, 'saas', 'issued', '2027-01-01', '2027-02-01', 'USD', 100, 0, 0, 100, 'staff', $2, $3,"
                " $4, now(), $2, current_date, current_date + 14, '{}'::jsonb, '{}'::jsonb, $5)"
                " RETURNING id, number",
                tid,
                NOTE,
                f"{NOTE}-{label}-key",
                f"AAS-2027-{'000901' if label == 'synthetic' else '000902'}",
                entity,
            )
            payment = await conn.fetchval(
                "INSERT INTO booking.agency_payment (invoice_id, amount, currency, method, received_on, document_ref,"
                " status, recorded_by, recorded_at, idempotency_key) VALUES ($1, 100, 'USD', 'bank_transfer',"
                " current_date, 'statement line 7', 'recorded', $2, now(), $3) RETURNING id",
                invoice["id"],
                NOTE,
                f"{NOTE}-{label}-pay",
            )
            out[label] = {"booking": booking, "invoice": invoice["id"], "number": invoice["number"], "payment": payment}
        departure = await conn.fetchval("SELECT id FROM booking.departures LIMIT 1")
        if departure is not None:
            out["departure_booking"] = await conn.fetchval(
                "INSERT INTO booking.supplier_bookings (departure_id, supplier_id, item_description, status,"
                " requested_at, created_by) VALUES ($1, $2, 'Coach for the group', 'requested', now(), $3)"
                " RETURNING id",
                departure,
                supplier,
                NOTE,
            )
        for t in GUARDED:
            await conn.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")
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


async def _answers(booking: asyncpg.Connection, rows: dict[str, Any]) -> dict[tuple[str, str], bool]:
    got: dict[tuple[str, str], bool] = {}
    for label in ("synthetic", "real"):
        r = rows[label]
        for route, params in (
            (e2e_staff.SUPPLIER_REQUEST[1], {"booking_id": str(r["booking"])}),
            (e2e_staff.SUPPLIER_CONFIRM[1], {"booking_id": str(r["booking"])}),
            (e2e_staff.PAYMENT_RECORD[1], {"ref": str(r["invoice"])}),
            (e2e_staff.PAYMENT_RECORD[1] + "#number", {"ref": r["number"]}),
            (e2e_staff.PAYMENT_APPROVE[1], {"payment_id": str(r["payment"])}),
            (e2e_staff.PAYMENT_REJECT[1], {"payment_id": str(r["payment"])}),
        ):
            got[(label, route)] = await e2e_staff.is_synthetic_record(booking, route.split("#")[0], params)
    return got


async def test_synthetic_agency_records_answer_true_and_real_ones_false(
    admin: asyncpg.Connection, booking: asyncpg.Connection
) -> None:
    await _stub(admin, CIS_401_COLUMNS)
    rows = await _seed(admin)
    got = await _answers(booking, rows)
    assert {k: v for k, v in got.items() if k[0] == "synthetic"} == {k: True for k in got if k[0] == "synthetic"}
    assert {k: v for k, v in got.items() if k[0] == "real"} == {k: False for k in got if k[0] == "real"}


async def test_records_of_no_agency_or_unknown_ids_answer_false(
    admin: asyncpg.Connection, booking: asyncpg.Connection
) -> None:
    await _stub(admin, CIS_401_COLUMNS)
    rows = await _seed(admin)
    if "departure_booking" in rows:
        assert not await e2e_staff.is_synthetic_record(
            booking, e2e_staff.SUPPLIER_CONFIRM[1], {"booking_id": str(rows["departure_booking"])}
        )
    unknown = str(uuid.uuid4())
    for route, param in (
        (e2e_staff.SUPPLIER_CONFIRM[1], "booking_id"),
        (e2e_staff.PAYMENT_RECORD[1], "ref"),
        (e2e_staff.PAYMENT_APPROVE[1], "payment_id"),
    ):
        assert not await e2e_staff.is_synthetic_record(booking, route, {param: unknown})
    assert not await e2e_staff.is_synthetic_record(booking, e2e_staff.PAYMENT_RECORD[1], {"ref": "AAS-2027-999999"})


async def test_without_cis_401_every_record_answers_false(
    admin: asyncpg.Connection, booking: asyncpg.Connection
) -> None:
    await _stub(admin, CIS_357_COLUMNS)
    await admin.execute(
        "INSERT INTO catalog.v_tenant_account_v1 VALUES ($1, 'Fernhollow Trips', 'active', NULL, true, 'agency', false,"
        " true)",
        SYNTHETIC,
    )
    rows = await _seed(admin)
    got = await _answers(booking, rows)
    assert not any(got.values())


async def test_without_the_view_every_record_answers_false(
    admin: asyncpg.Connection, booking: asyncpg.Connection
) -> None:
    await admin.execute(DROP)
    rows = await _seed(admin)
    got = await _answers(booking, rows)
    assert not any(got.values())
