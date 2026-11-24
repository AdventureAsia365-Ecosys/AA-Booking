"""AA-1175 / AA-1158 (S290, root ADR 0006a A10e-6): the record checks of the pricing pair's policy decisions and
supplier bills (:mod:`app.auth.e2e_staff_finance`) on real PostgreSQL, as role ``booking``.

``catalog.v_tenant_account_v1`` (CIS 357 + 401) is stood in by a plain table around each test. Covers:

- a policy decision answers True only for a ``price_pax_bands`` version the E2E owner proposed: another key the owner
  proposed, a band change of a real person or of another E2E identity, or an unknown version answer False;
- a bill entry answers True only for the maker's synthetic supplier and a synthetic agency's trip case (by UUID or
  code): a real supplier, a supplier of another person, a real agency's case or an unknown case answer False;
- a bill approval answers True only for a bill the maker entered and last edited, of its synthetic supplier, on a
  synthetic agency's case: a bill edited by someone else, entered by someone else, of a real supplier or on a real
  agency's case answers False.

All names are invented.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.auth import e2e_owner, e2e_staff

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CIS_401_COLUMNS = (
    "tenant_id uuid, name text, account_status text, suspend_reason text, in_service boolean, kind text, "
    "is_pilot boolean, trade_book boolean, is_synthetic boolean"
)
DROP = "DROP TABLE IF EXISTS catalog.v_tenant_account_v1"
SYNTHETIC = uuid.UUID("00000000-0000-4000-8000-0000000a1175")
REAL = uuid.UUID("00000000-0000-4000-8000-0000000b1175")
MAKER_KEY, OWNER_KEY, CHECKER_KEY, OTHER_KEY = "it-a10e6-maker", "it-a10e6-owner", "it-a10e6-checker", "it-a10e6-x"
KEYS = [MAKER_KEY, OWNER_KEY, CHECKER_KEY, OTHER_KEY]
TAG = "it-a10e6"
APPROVE = e2e_staff.POLICY_APPROVE[1]
REJECT = e2e_staff.POLICY_REJECT[1]
ENTER = e2e_staff.BILL_CREATE[1]
BILL_OK = e2e_staff.BILL_APPROVE[1]


async def _purge(conn: asyncpg.Connection) -> None:
    ours = f"(SELECT id FROM booking.suppliers WHERE company_name LIKE '%{TAG} %')"
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE bk_owner")
        await conn.execute(f"DELETE FROM booking.supplier_bills WHERE supplier_id IN {ours}")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.suppliers WHERE company_name LIKE '%{TAG} %'")  # noqa: S608
        await conn.execute("DELETE FROM booking.trip_cases WHERE notes = $1", TAG)
        await conn.execute(f"DELETE FROM booking.b2b_partners WHERE agency_name LIKE '{TAG} %'")  # noqa: S608
        await conn.execute("DELETE FROM booking.pricing_policy WHERE created_by = ANY($1::text[])", KEYS)
        await conn.execute("DELETE FROM booking.staff_users WHERE cis_user_id = ANY($1::text[])", KEYS)
    await conn.execute(DROP)


async def _policy(conn: asyncpg.Connection, key: str, creator: str) -> str:
    """A new version of ``key`` on top of its latest one (or version 1), made by ``creator``."""
    last = await conn.fetchrow(
        "SELECT id, version_no FROM booking.pricing_policy WHERE param_key = $1 ORDER BY version_no DESC LIMIT 1", key
    )
    return await conn.fetchval(
        "INSERT INTO booking.pricing_policy (param_key, version_no, value, effective_from, reason, replaces_id,"
        " created_by) VALUES ($1, $2, $3::jsonb, DATE '2031-01-01', 'invented band check', $4, $5) RETURNING id::text",
        key,
        (last["version_no"] + 1) if last else 1,
        json.dumps([[1, 1], [2, None]]),
        last["id"] if last else None,
        creator,
    )


async def _bill(conn: asyncpg.Connection, supplier: str, case: str, maker: str, n: int) -> dict[str, str]:
    """A received bill entered by ``maker`` (the bk-045 trigger makes the maker its facts_edited_by)."""
    row = await conn.fetchrow(
        "INSERT INTO booking.supplier_bills (supplier_id, trip_case_id, bill_number, currency, amount, issue_date,"
        " due_date, idempotency_key, request_sha256, created_by, facts_edited_by, updated_by)"
        " VALUES ($1::uuid, $2::uuid, $3, 'USD', 60, $4, $4, $5, $6, $7, $7, $7) RETURNING id::text, code",
        supplier,
        case,
        f"SYNTHETIC-{TAG}-{n}",
        date(2031, 3, 1),
        f"{TAG}-key-{n}",
        "d" * 64,
        maker,
    )
    return dict(row)


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
        for key, email in (
            (MAKER_KEY, e2e_staff.E2E_PRICING_MAKER_EMAIL),
            (OWNER_KEY, e2e_owner.E2E_OWNER_EMAIL),
            (CHECKER_KEY, e2e_staff.E2E_PRICING_CHECKER_EMAIL),
            (OTHER_KEY, "lena@adventure.asia"),
        ):
            await conn.execute(
                "INSERT INTO booking.staff_users (cis_user_id, role, active, name, email)"
                " VALUES ($1, 'contracting', true, $1, $2)",
                key,
                email,
            )
        out["owner_bands"] = await _policy(conn, "price_pax_bands", OWNER_KEY)
        out["real_bands"] = await _policy(conn, "price_pax_bands", OTHER_KEY)
        out["checker_bands"] = await _policy(conn, "price_pax_bands", CHECKER_KEY)
        out["owner_other_key"] = await _policy(conn, "it_a10e6_other_param", OWNER_KEY)
        for case, name, creator in (
            ("ground", f"SYNTHETIC {TAG} ground DMC", MAKER_KEY),
            ("real", f"{TAG} Lantern DMC", OTHER_KEY),
            ("someone", f"SYNTHETIC {TAG} someone's DMC", OTHER_KEY),
        ):
            out[case] = await conn.fetchval(
                "INSERT INTO booking.suppliers (company_name, categories, country_code, created_by, updated_by)"
                " VALUES ($1, ARRAY['transport'], 'TH', $2, $2) RETURNING id::text",
                name,
                creator,
            )
        for label, tid in (("synthetic", SYNTHETIC), ("real", REAL)):
            await conn.execute(
                "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
                " VALUES ($1, $2, 'active', 'USD', 'a10e6client')",
                tid,
                f"{TAG} {label}",
            )
            out[f"case_{label}"] = dict(
                await conn.fetchrow(
                    "INSERT INTO booking.trip_cases (channel, party_tenant_id, status, start_date, end_date, pax,"
                    " currency, source_system, notes) VALUES ('b2b', $1, 'confirmed', $2, $3, 2, 'USD', 'crm', $4)"
                    " RETURNING id::text, code",
                    tid,
                    date(2031, 3, 1),
                    date(2031, 3, 5),
                    TAG,
                )
            )
        synthetic_case, real_case = out["case_synthetic"]["id"], out["case_real"]["id"]
        out["bill_ok"] = await _bill(conn, out["ground"], synthetic_case, MAKER_KEY, 1)
        out["bill_edited"] = await _bill(conn, out["ground"], synthetic_case, MAKER_KEY, 2)
        await conn.execute(  # someone else changed its amount after the maker entered it
            "UPDATE booking.supplier_bills SET amount = 70, facts_edited_by = $2, updated_by = $2 WHERE id = $1::uuid",
            out["bill_edited"]["id"],
            OTHER_KEY,
        )
        out["bill_someone"] = await _bill(conn, out["ground"], synthetic_case, OTHER_KEY, 3)
        out["bill_real_supplier"] = await _bill(conn, out["real"], synthetic_case, MAKER_KEY, 4)
        out["bill_real_case"] = await _bill(conn, out["ground"], real_case, MAKER_KEY, 5)
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


async def test_a_decision_reaches_only_the_owner_s_band_change(
    booking: asyncpg.Connection, seeded: dict[str, Any]
) -> None:
    check = e2e_staff.is_synthetic_record
    answers = {
        name: (
            await check(booking, APPROVE, {"version_id": seeded[name]}),
            await check(booking, REJECT, {"version_id": seeded[name]}),
        )
        for name in ("owner_bands", "real_bands", "checker_bands", "owner_other_key")
    }
    answers["unknown"] = (
        await check(booking, APPROVE, {"version_id": str(uuid.uuid4())}),
        await check(booking, REJECT, {"version_id": str(uuid.uuid4())}),
    )
    assert answers == {
        "owner_bands": (True, True),
        "real_bands": (False, False),
        "checker_bands": (False, False),
        "owner_other_key": (False, False),
        "unknown": (False, False),
    }


def _body(supplier: str, case: str) -> dict[str, Any]:
    return {"supplier_id": supplier, "trip_case": case, "bill_number": "SYNTHETIC-x", "lines": []}


async def test_a_bill_needs_the_maker_s_synthetic_supplier_and_a_synthetic_case(
    booking: asyncpg.Connection, seeded: dict[str, Any]
) -> None:
    ground, real, someone = seeded["ground"], seeded["real"], seeded["someone"]
    synthetic, real_case = seeded["case_synthetic"], seeded["case_real"]
    check = e2e_staff.is_synthetic_record
    answers = {
        "by_id": await check(booking, ENTER, {}, _body(ground, synthetic["id"])),
        "by_code": await check(booking, ENTER, {}, _body(ground, synthetic["code"].lower())),
        "real_supplier": await check(booking, ENTER, {}, _body(real, synthetic["id"])),
        "someone_s_supplier": await check(booking, ENTER, {}, _body(someone, synthetic["id"])),
        "real_case": await check(booking, ENTER, {}, _body(ground, real_case["id"])),
        "real_case_code": await check(booking, ENTER, {}, _body(ground, real_case["code"])),
        "unknown_case": await check(booking, ENTER, {}, _body(ground, str(uuid.uuid4()))),
    }
    assert answers == {
        "by_id": True,
        "by_code": True,
        "real_supplier": False,
        "someone_s_supplier": False,
        "real_case": False,
        "real_case_code": False,
        "unknown_case": False,
    }


async def test_a_bill_approval_reaches_only_the_maker_s_synthetic_bill(
    booking: asyncpg.Connection, seeded: dict[str, Any]
) -> None:
    check = e2e_staff.is_synthetic_record
    names = ("bill_ok", "bill_edited", "bill_someone", "bill_real_supplier", "bill_real_case")
    answers = {name: await check(booking, BILL_OK, {"bill": seeded[name]["id"]}) for name in names}
    answers["by_code"] = await check(booking, BILL_OK, {"bill": seeded["bill_ok"]["code"]})
    answers["unknown"] = await check(booking, BILL_OK, {"bill": str(uuid.uuid4())})
    assert answers == {
        "bill_ok": True,
        "bill_edited": False,
        "bill_someone": False,
        "bill_real_supplier": False,
        "bill_real_case": False,
        "by_code": True,
        "unknown": False,
    }
