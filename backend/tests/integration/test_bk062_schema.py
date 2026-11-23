"""AA-1155 (bk-062) on a real PostgreSQL: Booking custom staff roles, their capabilities, the key-only view CIS reads
and custom codes on staff_users.

Registration and re-run no-op; the seed is the exact "Product, ops & accounting" allow-list; the approve class and
malformed names are refused; codes and bases are checked; a role in use by a capability row goes with its role; the
view has its five columns, is read-only for booking and readable by cis_app, the tables are not; staff_users takes a
built-in role or a custom code and nothing else. All names are invented.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import run_062_keeping_later_caps

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "062_custom_staff_roles.sql"
SEED = "custom_product_ops_accounting"
SEED_CAPS = {
    "supply.read", "supply.write", "pricing.read", "pricing.write", "trip_plan.write", "stop_sale.write",
    "catalog.read", "ops.read", "ops.write", "b2b_desk.read", "b2b_desk.write", "staff.directory",
    "bill.read", "bill.write", "billing.read", "billing.write", "agency_billing.read", "agency_billing.write",
    "profit.read", "b2b_desk.book", "trip_case.read", "trip_case.write", "customer.read", "customer.write",
}  # fmt: skip
LATER_CAPS = {"supplier_bank.write"}  # bk-067 (AA-1158 part 2, Q10)
ZZ = "custom_zz_lantern"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def app(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def clean(app: asyncpg.Connection) -> AsyncIterator[None]:
    yield
    await app.execute("DELETE FROM booking.staff_role WHERE code LIKE 'custom_zz%'")
    await app.execute("DELETE FROM booking.staff_users WHERE cis_user_id LIKE 'zz-bk062-%'")


async def test_registered_once_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    before = await admin.fetch("SELECT role_code, capability FROM booking.staff_role_capability ORDER BY 1, 2")
    await admin.execute("SET ROLE bk_owner")
    # bk-067 added supplier_bank.write to the seed role; the bk-062 post-check counts its own 24 (conftest helper).
    await run_062_keeping_later_caps(admin, MIGRATION.read_text(encoding="utf-8"))
    await admin.execute("RESET ROLE")
    after = await admin.fetch("SELECT role_code, capability FROM booking.staff_role_capability ORDER BY 1, 2")
    assert [dict(r) for r in after] == [dict(r) for r in before]
    assert await admin.fetchval("SELECT count(*) AS n FROM booking.v_schema_versions WHERE version = 'bk-062'") == 1


async def test_contract_file_without_a_directive() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert text.startswith("-- Migration bk-062")
    assert "-- migrate:" not in text


async def test_seed_is_the_exact_allow_list(admin: asyncpg.Connection) -> None:
    role = await admin.fetchrow("SELECT label, based_on, archived FROM booking.staff_role WHERE code = $1", SEED)
    assert dict(role) == {"label": "Product, ops & accounting", "based_on": "contracting", "archived": False}
    caps = {r["capability"] for r in await admin.fetch(
        "SELECT capability FROM booking.staff_role_capability WHERE role_code = $1", SEED)}  # fmt: skip
    # bk-067 (AA-1158 part 2, Q10) adds the entry of supplier bank accounts, never their read or approval.
    assert caps == SEED_CAPS | LATER_CAPS
    assert not caps & {"customer.admin", "trip_case.assign", "version.write", "payout.write", "billing_config.write"}
    assert not any(c.endswith(".approve") for c in caps)
    assert {c for c in caps if c.startswith("supplier_bank.")} == {"supplier_bank.write"}


@pytest.mark.parametrize(
    "capability",
    [
        "bill.approve", "b2b_desk.approve", "pricing_policy.approve_finance", "supplier.activate", "contract.publish",
        "stop_sale.approve", "payout.write", "payment.verify", "billing_config.write", "staff.admin", "pricing.approve",
        "Bill.Read", "bill", "bill.read;x", "a.b.c.d.e",
    ],
)  # fmt: skip
async def test_approve_class_and_malformed_capabilities_are_refused(
    app: asyncpg.Connection, clean: None, capability: str
) -> None:
    await app.execute(
        "INSERT INTO booking.staff_role (code, label, based_on, created_by, updated_by)"
        " VALUES ($1, 'Zz role', 'sales', 'zz-actor', 'zz-actor')",
        ZZ,
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await app.execute(
            "INSERT INTO booking.staff_role_capability (role_code, capability, created_by) VALUES ($1, $2, 'zz-actor')",
            ZZ,
            capability,
        )


@pytest.mark.parametrize(
    ("code", "based_on"),
    [("sales", "sales"), ("custom_", "sales"), ("custom_Zz", "sales"), ("custom_zz", "owner"), ("custom_zz", "")],
)
async def test_codes_and_bases_are_checked(app: asyncpg.Connection, clean: None, code: str, based_on: str) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await app.execute(
            "INSERT INTO booking.staff_role (code, label, based_on, created_by, updated_by)"
            " VALUES ($1, 'Zz role', $2, 'zz-actor', 'zz-actor')",
            code,
            based_on,
        )


async def test_booking_edits_a_role_and_its_capabilities_go_with_it(app: asyncpg.Connection, clean: None) -> None:
    await app.execute(
        "INSERT INTO booking.staff_role (code, label, based_on, created_by, updated_by)"
        " VALUES ($1, 'Zz role', 'finance', 'zz-actor', 'zz-actor')",
        ZZ,
    )
    await app.execute(
        "INSERT INTO booking.staff_role_capability (role_code, capability, created_by)"
        " VALUES ($1, 'bill.read', 'zz-actor'), ($1, 'profit.read', 'zz-actor')",
        ZZ,
    )
    await app.execute("UPDATE booking.staff_role SET archived = true, updated_by = 'zz-actor' WHERE code = $1", ZZ)
    rows = await app.fetch(
        "SELECT capability, archived FROM booking.v_staff_role_capability WHERE role_code = $1 ORDER BY 1", ZZ
    )
    assert [(r["capability"], r["archived"]) for r in rows] == [("bill.read", True), ("profit.read", True)]
    await app.execute("DELETE FROM booking.staff_role WHERE code = $1", ZZ)
    assert await app.fetchval("SELECT count(*) AS n FROM booking.staff_role_capability WHERE role_code = $1", ZZ) == 0


async def test_view_columns_and_readers(admin: asyncpg.Connection, app: asyncpg.Connection, clean: None) -> None:
    columns = [
        r["attname"]
        for r in await admin.fetch(
            "SELECT attname FROM pg_attribute WHERE attrelid = 'booking.v_staff_role_capability'::regclass"
            " AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
        )
    ]
    assert columns == ["role_code", "label", "based_on", "archived", "capability"]
    privilege = "SELECT has_table_privilege($1, $2, $3) AS ok"
    expected = {
        ("cis_app", "booking.v_staff_role_capability", "SELECT"): True,
        ("cis_app", "booking.v_staff_role_capability", "INSERT"): False,
        ("cis_app", "booking.staff_role", "SELECT"): False,
        ("cis_app", "booking.staff_role_capability", "SELECT"): False,
        ("booking", "booking.v_staff_role_capability", "SELECT"): True,
        ("booking", "booking.v_staff_role_capability", "INSERT"): False,
        ("booking", "booking.staff_role", "INSERT"): True,
    }
    for (role, obj, priv), ok in expected.items():
        assert (await admin.fetchrow(privilege, role, obj, priv))["ok"] is ok, (role, obj, priv)
    await app.execute(
        "INSERT INTO booking.staff_role (code, label, based_on, created_by, updated_by)"
        " VALUES ($1, 'Zz empty role', 'sales', 'zz-actor', 'zz-actor')",
        ZZ,
    )
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE cis_app")
        rows = await admin.fetch(
            "SELECT role_code, capability FROM booking.v_staff_role_capability WHERE role_code IN ($1, $2)", SEED, ZZ
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await admin.fetch("SELECT code FROM booking.staff_role LIMIT 1")
    by_role: dict[str, set[str | None]] = {}
    for r in rows:
        by_role.setdefault(r["role_code"], set()).add(r["capability"])
    assert by_role == {SEED: SEED_CAPS | LATER_CAPS, ZZ: {None}}


@pytest.mark.parametrize(
    ("role", "ok"),
    [("finance", True), (SEED, True), ("custom_zz_any", True), ("custom", False), ("Custom_zz", False),
     ("approver", False)],
)  # fmt: skip
async def test_staff_users_takes_built_in_roles_and_custom_codes(
    app: asyncpg.Connection, clean: None, role: str, ok: bool
) -> None:
    insert = "INSERT INTO booking.staff_users (cis_user_id, role, active) VALUES ($1, $2, true)"
    if ok:
        await app.execute(insert, f"zz-bk062-{role}", role)
    else:
        with pytest.raises(asyncpg.CheckViolationError):
            await app.execute(insert, f"zz-bk062-{role}", role)
