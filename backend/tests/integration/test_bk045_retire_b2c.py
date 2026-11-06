"""bk-045 (AA-1046 R4b part 1; AA-1037 S270 DRY RUN) on a real PostgreSQL: the B2C drop and its DRY RUN script.

The Booking conftest replays every migration, bk-045 included, so the session database has no B2C table. This module
rebuilds the schema up to bk-044 (``apply_migrations(only=range(1, 45))``), builds a B2C graph with the integration
helpers (invented names only: a sold b2c case with an approved version, a sent invoice, a verified deposit with its
evidence file, a confirmed supplier booking, an approved bill and a payout; a second b2c case with a customer that has
an identity, an OTP, a session and a profile picture; an inquiry inbox row), plus a B2B case, a B2B supplier booking
and a CIS catalog inbox row that must survive. Then:

- the DRY RUN script (``tests/integration/b2c_dry_run.py``, a byte copy of root
  ``docs/audits/2026-11-05-S270-aa1037-b2c-drop-dryrun/scripts/aa1037_b2c_dry_run.py``, the one the orchestrator runs
  in the container) pins the dropped tables (row count + an md5 of the non-personal columns), counts the kept b2c
  graph and says "not ready" while the rows are fresh;
- bk-045 refuses: fresh B2C rows (30-day guard, including a fresh supplier payout, bill line, OTP use or operations
  file of a b2c case), unfilled pins, a moved count, a changed pinned code, moved ids, a queued intake job;
- its 30-day block, pinned table list and pinned columns are the DRY RUN script's, word for word;
- with the rows backdated 40 days and the pins pasted, it applies: the customer and customer money tables are gone,
  every money row is in booking.retired_b2c_record (whole row, retain_until 10 years on), the b2c cases with their
  supplier bookings, bills, payouts, files, audit and notification rows stay, a new b2c case is refused, the B2B rows
  and the CIS feed rows stay, the rewritten views and guards still work, and a second run is a no-op.

The module ends with the full replay again, so later modules see the session database as it was.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.integration import ops
from tests.integration.conftest import STUB_CATALOG, STUB_TRIPPLANNER, _reset_and_migrate, apply_migrations

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

HERE = Path(__file__).resolve().parent
DRAFT = HERE.parents[2] / "migrations" / "045_aa1037_retire_b2c.sql"
DRY_RUN = HERE / "b2c_dry_run.py"
# The root repo (AA-Ecosys, this repo at apps/AA-Booking; AA_ECOSYS_ROOT overrides it): its copy of the DRY RUN script,
# its pilot PII guard and its schema reference. The tests that need it skip without it (CI checks out this repo only).
ROOT = Path(os.environ.get("AA_ECOSYS_ROOT") or HERE.parents[4])
ROOT_DRY_RUN = ROOT / "docs/audits/2026-11-05-S270-aa1037-b2c-drop-dryrun/scripts/aa1037_b2c_dry_run.py"
VERSION = "bk-045"
STAFF_A, STAFF_B = "it-1037-staff-a", "it-1037-staff-b"
BUCKET = "aa-booking-dev-ops-docs-" + "0" * 12
FEED_REF = "it-1037-catalog"  # the CIS catalog feed inbox row that must survive
PINS_RE = re.compile(r"pins jsonb := '(\{.*?\})';", re.S)
LAST_WRITE_RE = re.compile(r"-- last-write:begin\n(.*?)\n\s*-- last-write:end", re.S)
PIN_TABLES_RE = re.compile(r"-- pin-tables:begin\n(.*?)\n\s*-- pin-tables:end", re.S)
PIN_COLUMNS_RE = re.compile(r"-- pin-columns:begin\n\s*pin_columns jsonb := '(\{.*?\})';\n\s*-- pin-columns:end", re.S)
PII_GUARD = ROOT / ".claude" / "hooks" / "pilot_pii_guard.py"
B2C_TABLES = (
    "booking.customers",
    "booking.customer_identities",
    "booking.customer_otps",
    "booking.customer_sessions",
    "booking.media_files",
    "booking.invoices",
    "booking.invoice_items",
    "booking.payment_schedule",
    "booking.payments",
    "booking.v_invoice_balance",
    "booking.v_payment_schedule_status",
    "booking.v_customer_summary",
    "booking.v_ops_inquiry_inbox",
    "booking.customer_code_seq",
    "booking.invoice_number_seq",
    "booking.payment_ref_seq",
)
BACKDATE = (
    "booking.customers",
    "booking.customer_identities",
    "booking.customer_otps",
    "booking.customer_sessions",
    "booking.media_files",
    "booking.invoices",
    "booking.invoice_items",
    "booking.payment_schedule",
    "booking.payments",
    "booking.trip_cases",
    "booking.trip_case_versions",
    "booking.supplier_bookings",
    "booking.supplier_bills",
    "booking.supplier_bill_items",
    "booking.supplier_payouts",
    "booking.ops_files",
)
WRITE_TIME_COLUMNS = ("created_at", "updated_at", "revoked_at", "used_at", "removed_at", "deleted_at")


def _dry_run() -> ModuleType:
    spec = importlib.util.spec_from_file_location("aa1037_b2c_dry_run", DRY_RUN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _draft(pins: dict[str, Any] | None = None) -> str:
    sql = DRAFT.read_text()
    if pins is not None:
        sql, n = PINS_RE.subn(lambda _m: f"pins jsonb := '{json.dumps(pins)}';", sql)
        assert n == 1
    return sql


async def _apply(conn: asyncpg.Connection, sql: str) -> None:
    """Run the draft the way DB Migrate does: as the live owner of schema booking."""
    await conn.execute('SET ROLE "bk_owner"')
    try:
        await conn.execute(sql)
    except BaseException:
        await conn.execute("ROLLBACK")
        raise
    finally:
        await conn.execute("RESET ROLE")


async def _backdate(conn: asyncpg.Connection, days: int = 40) -> None:
    """Every B2C write time `days` back (the user triggers would reset updated_at, so they are off meanwhile)."""
    async with conn.transaction():
        await conn.execute('SET LOCAL ROLE "bk_owner"')
        for table in BACKDATE:
            cols = {
                r["attname"]
                for r in await conn.fetch(
                    "SELECT attname FROM pg_attribute WHERE attrelid = $1::regclass AND attnum > 0"
                    " AND NOT attisdropped",
                    table,
                )
            }
            sets = ", ".join(f"{c} = {c} - make_interval(days => {days})" for c in WRITE_TIME_COLUMNS if c in cols)
            await conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
            await conn.execute(f"UPDATE {table} SET {sets}")  # noqa: S608 — constant names
            await conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
        await conn.execute(
            "UPDATE booking.inbox_events SET received_at = received_at - make_interval(days => $1)"
            " WHERE contract = 'inquiry.submitted'",
            days,
        )


async def _schema_before_bk045(conn: asyncpg.Connection) -> None:
    """The session database as it was before bk-045 (conftest._reset_and_migrate up to bk-044)."""
    await conn.execute("DROP SCHEMA IF EXISTS booking CASCADE")
    await conn.execute("DROP SCHEMA IF EXISTS tripplanner CASCADE")
    await conn.execute("DROP SCHEMA IF EXISTS catalog CASCADE")
    await conn.execute("DELETE FROM shared.schema_versions WHERE version LIKE 'bk-%'")
    await apply_migrations(conn, first_pass=True, only=range(1, 45))
    await conn.execute(STUB_TRIPPLANNER.read_text(encoding="utf-8"))
    await conn.execute(STUB_CATALOG.read_text(encoding="utf-8"))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def world(migrated_admin_dsn: str) -> AsyncIterator[dict[str, Any]]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _schema_before_bk045(conn)
    await ops.purge_ops(conn)
    await conn.execute("DELETE FROM booking.inbox_events WHERE idempotency_key LIKE 'it-1037-%'")
    await ops.staff(conn, (STAFF_A, "admin"), (STAFF_B, "admin"))
    sup = await ops.supply(conn, "Lantern")

    # b2c case 1: sold, invoiced, a verified deposit with evidence, a supplier booking billed and paid out
    sold = await ops.sold_case(conn, sup, tag="1037-sold")
    inv = await ops.draft_invoice(conn, sold, by=STAFF_A, tag="1037-inv")
    await ops.send_invoice(conn, inv["id"], by=STAFF_A)
    pay = await ops.record_payment(conn, inv["id"], "720.00", by=STAFF_A, method="bank_transfer", tag="1037-dep")
    file_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO booking.ops_files (id, kind, entity_type, entity_id, trip_case_id, s3_bucket, s3_key,"
        " content_type, bytes, sha256, file_name, uploaded_by) VALUES ($1::uuid, 'payment_evidence', 'payment',"
        " $2::uuid, $3::uuid, $4, $5, 'application/pdf', 10, $6, 'slip.pdf', $7)",
        file_id,
        pay,
        sold["id"],
        BUCKET,
        f"ops-docs/payment_evidence/{file_id}.pdf",
        "a" * 64,
        STAFF_A,
    )
    await conn.execute("UPDATE booking.payments SET evidence_file_id = $2::uuid WHERE id = $1::uuid", pay, file_id)
    await ops.verify_payment(conn, pay, by=STAFF_B)
    booking = await ops.confirmed_booking(conn, sold, sup["supplier"], item=sold["room_item"])
    sbill = await ops.bill(
        conn,
        sup["supplier"],
        by=STAFF_A,
        number="LAN-1",
        lines=((booking, "300.00"),),
        amount="300.00",
        tag="1037-bill",
    )
    await ops.approve_bill(conn, sbill["id"], by=STAFF_B)
    await ops.payout(conn, sbill["id"], "300.00", by=STAFF_B, tag="1037-payout")

    # b2c case 2: a signed-in traveller (identity, OTP, session, profile picture) from an inquiry hand-off
    lead = await ops.b2c_case(conn, tag="1037-lead")
    customer = await conn.fetchval("SELECT customer_id::text FROM booking.trip_cases WHERE id = $1::uuid", lead["id"])
    media = await conn.fetchval(
        "INSERT INTO booking.media_files (s3_bucket, s3_key, content_type) VALUES ($1, 'profiles/it-1037.jpg',"
        " 'image/jpeg') RETURNING id::text",
        BUCKET,
    )
    await conn.execute("UPDATE booking.customers SET profile_media_id = $2::uuid WHERE id = $1::uuid", customer, media)
    await conn.execute(
        "INSERT INTO booking.customer_identities (iss, sub, customer_id) VALUES ('https://idp.example.test',"
        " 'it-1037-sub', $1::uuid)",
        customer,
    )
    await conn.execute(
        "INSERT INTO booking.customer_otps (customer_id, otp_hash, channel, expires_at)"
        " VALUES ($1::uuid, 'not-a-real-hash', 'email', now() + interval '10 minutes')",
        customer,
    )
    await conn.execute(
        "INSERT INTO booking.customer_sessions (customer_id, refresh_token_hash, expires_at)"
        " VALUES ($1::uuid, 'not-a-real-token-hash', now() + interval '1 day')",
        customer,
    )
    event = await conn.fetchval(
        "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload, status, processed_at)"
        " VALUES ('tripplanner', 'inquiry.submitted', 'it-1037-inquiry', '{}'::jsonb, 'processed', now())"
        " RETURNING id::text"
    )
    await conn.execute("UPDATE booking.trip_cases SET inbox_event_id = $2::uuid WHERE id = $1::uuid", lead["id"], event)
    await conn.execute(
        "INSERT INTO booking.audit_logs (actor_type, action, entity_type, entity_id) VALUES"
        " ('staff', 'update', 'customer', $1::uuid), ('staff', 'update', 'trip_case', $2::uuid)",
        customer,
        lead["id"],
    )

    # B2B rows that must survive
    b2b = await ops.b2b_case(conn, tag="1037-b2b")
    b2b_booking = await conn.fetchval(
        "INSERT INTO booking.supplier_bookings (trip_case_id, supplier_id, item_description, net_amount, net_currency)"
        " VALUES ($1::uuid, $2::uuid, 'Agency room', 100, 'USD') RETURNING id::text",
        b2b["id"],
        sup["supplier"],
    )
    await conn.execute(
        "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload, status, processed_at)"
        " VALUES ('cis', 'catalog.tour', $1, '{}'::jsonb, 'processed', now())",
        FEED_REF,
    )
    await conn.execute(
        "INSERT INTO booking.audit_logs (actor_type, action, entity_type, entity_id) VALUES"
        " ('staff', 'update', 'trip_case', $1::uuid)",
        b2b["id"],
    )
    try:
        yield {"conn": conn, "b2b": b2b, "b2b_booking": b2b_booking, "sold": sold, "lead": lead}
    finally:
        await conn.close()
        await _reset_and_migrate(migrated_admin_dsn)  # the full replay (bk-045 included) for the next modules


EXPECTED_ROWS = {
    "customers": 2,
    "customer_identities": 1,
    "customer_otps": 1,
    "customer_sessions": 1,
    "media_files": 1,
    "invoices": 1,
    "invoice_items": 1,
    "payment_schedule": 2,
    "payments": 1,
}
EXPECTED_KEPT = {
    "b2c_trip_cases": 2,
    "b2c_versions": 1,
    "b2c_supplier_bookings": 1,
    "b2c_supplier_bills": 1,
    "b2c_supplier_payouts": 1,
    "b2c_ops_files": 1,
    "inquiry_inbox_events": 1,
}
MONEY_TABLES = ("invoices", "invoice_items", "payment_schedule", "payments")
LAN_BILL = "SELECT id FROM booking.supplier_bills WHERE bill_number = 'LAN-1'"
# Writes on kept b2c rows that the 30-day guard must see (each sets one write time to now, triggers off).
FRESH_KEPT_WRITES = (
    f"UPDATE booking.supplier_payouts SET updated_at = now() WHERE bill_id IN ({LAN_BILL})",  # noqa: S608
    f"UPDATE booking.supplier_bill_items SET created_at = now() WHERE bill_id IN ({LAN_BILL})",  # noqa: S608
    "UPDATE booking.customer_otps SET used_at = now()",
    "UPDATE booking.ops_files SET updated_at = now() WHERE entity_type = 'payment'",
)


def _norm(sql: str) -> str:
    return " ".join(sql.split())


async def _pins(conn: asyncpg.Connection) -> dict[str, Any]:
    return (await _dry_run().measure(conn))["pins"]


async def _touch(conn: asyncpg.Connection, sql: str) -> None:
    """One write as bk_owner with the user triggers of its table off (so no guard or updated_at trigger interferes)."""
    table = re.match(r"UPDATE (\S+)", sql).group(1)  # type: ignore[union-attr]
    async with conn.transaction():
        await conn.execute('SET LOCAL ROLE "bk_owner"')
        await conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        await conn.execute(sql)
        await conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")


async def test_dry_run_matches_the_draft() -> None:
    dry = _dry_run()
    draft = DRAFT.read_text()
    block = LAST_WRITE_RE.search(draft)
    assert block
    migration_sql = _norm(block.group(1)).replace("max(t) INTO last_at FROM", "max(t) AS last_write FROM")
    assert migration_sql.removesuffix(";") == _norm(dry.LAST_WRITE_SQL)
    tables = PIN_TABLES_RE.search(draft)
    assert tables
    assert re.findall(r"'([a-z_]+)'", tables.group(1)) == list(dry.PIN_TABLES)
    placeholder = PINS_RE.search(draft)
    assert placeholder
    assert json.loads(placeholder.group(1)) == dict.fromkeys(dry.PIN_TABLES)
    for statement in dry.HASH_SETTINGS:
        assert f"{statement};" in draft, statement
    # the pinned columns are the DRY RUN's, and each written-out pin statement is the template over them
    columns = PIN_COLUMNS_RE.search(draft)
    assert columns
    assert json.loads(columns.group(1)) == dry.PIN_COLUMNS
    assert list(dry.PIN_SQL) == list(dry.PIN_TABLES) == list(dry.PIN_COLUMNS)
    for table, sql in dry.PIN_SQL.items():
        assert sql == dry.PIN_SQL_TEMPLATE.format(cols=dry.PIN_COLUMNS[table], table=table), table
    # the migration's EXECUTE is the same template (%s = the columns, %I = the table)
    template = dry.PIN_SQL_TEMPLATE.replace("''", "%L").replace("{cols}", "%s").replace("{table}", "%I")
    execute = re.search(r"EXECUTE format\('(SELECT count\(\*\) AS n, md5.*?)',\s*'', pin_columns", draft, re.S)
    assert execute
    assert re.sub(r"'\s+'", "", execute.group(1)) == template


async def test_dry_run_is_a_byte_copy_of_the_root_script() -> None:
    if not ROOT_DRY_RUN.exists():
        pytest.skip("the root repo is not checked out next to this one")
    assert DRY_RUN.read_bytes() == ROOT_DRY_RUN.read_bytes(), "copy the root DRY RUN script here byte for byte"


async def test_pins_read_no_personal_column() -> None:
    """No pin reads a column the schema reference tags pii: (the pilot PII guard would refuse the ECS exec)."""
    ref = ROOT / "docs" / "architecture" / "db-schema-reference.md"
    if not ref.exists():
        pytest.skip("the root repo is not checked out next to this one")
    reference = ref.read_text()
    for table, cols in _dry_run().PIN_COLUMNS.items():
        block = re.search(rf"^### `booking\.{table}`.*?\n```\n(.*?)\n```", reference, re.S | re.M)
        assert block, table
        personal = {line.split(" ", 1)[0] for line in block.group(1).splitlines() if "pii:" in line}
        pinned = {c.strip().removeprefix("t.") for c in cols.split(",")}
        assert not pinned & personal, (table, pinned & personal)


@pytest.mark.skipif(not PII_GUARD.exists(), reason="the root repo's pilot PII guard is not next to this folder")
async def test_pilot_pii_guard_accepts_the_dry_run() -> None:
    """The PreToolUse hook passes the ECS exec of this script (exit 0) and refuses a whole-row read (exit 2)."""
    import subprocess
    import sys

    def run(script: Path) -> int:
        command = f"ECSX_SERVICE=aa-booking-dev-api tools/ecs/{'ecs' + 'x.sh'} {script}"  # the orchestrator's call
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)}
        done = subprocess.run(  # noqa: S603 — fixed interpreter and hook path
            [sys.executable, str(PII_GUARD)], input=payload, text=True, capture_output=True, env=env, cwd=ROOT
        )
        return done.returncode

    assert run(DRY_RUN) == 0
    probe = DRY_RUN.with_name("_pii_probe_whole_row.py")
    probe.write_text('SQL = "SELECT md5(t::text) AS h FROM booking.customers t"\n')
    try:
        assert run(probe) == 2
    finally:
        probe.unlink()


async def test_dry_run_counts_the_b2c_graph(world: dict[str, Any]) -> None:
    dry = _dry_run()
    report = await dry.measure(world["conn"])
    assert {k: v["n"] for k, v in report["pins"].items()} == EXPECTED_ROWS
    assert all(re.fullmatch(r"[0-9a-f]{32}", v["md5"]) for v in report["pins"].values())
    assert report["kept"] == EXPECTED_KEPT
    assert report["breakdowns"]["trip_cases_by_channel"] == {"b2b": 1, "b2c": 2}
    assert report["breakdowns"]["b2c_ops_files_by_kind"] == {"payment_evidence:active": 1}
    assert report["guard_30d_passes"] is False
    verdict = dry.verdict(report)
    assert not verdict["ready"]
    assert any("days" in r for r in verdict["reasons"]), verdict
    # the only views on the dropped objects are the ones the draft drops or rewrites
    views = {d["dependent"] for d in report["dependents"] if d["kind"] in ("view", "view_on_column")}
    assert views <= {
        "booking.v_customer_summary",
        "booking.v_invoice_balance",
        "booking.v_payment_schedule_status",
        "booking.v_trip_check_board",
        "booking.v_ops_data_totals",
    }, views
    # the same rows give the same pins (the migration compares them byte for byte)
    assert (await dry.measure(world["conn"]))["pins"] == report["pins"]


async def test_draft_refuses_fresh_b2c_rows(world: dict[str, Any]) -> None:
    with pytest.raises(asyncpg.RaiseError, match="less than 30 days ago"):
        await _apply(world["conn"], _draft(await _pins(world["conn"])))


async def test_draft_refuses_without_pins_on_a_moved_count_and_on_changed_rows(world: dict[str, Any]) -> None:
    conn = world["conn"]
    await _backdate(conn)
    pins = await _pins(conn)
    with pytest.raises(asyncpg.RaiseError, match="has no DRY RUN pin"):
        await _apply(conn, _draft())
    with pytest.raises(asyncpg.RaiseError, match="the DRY RUN counted"):
        await _apply(conn, _draft({**pins, "payments": {**pins["payments"], "n": 2}}))
    # same count, a pinned code changed, write time untouched (triggers off): only the pin hash sees it
    await _touch(
        conn,
        "UPDATE booking.customers SET verified = NOT verified"
        " WHERE id = (SELECT id FROM booking.customers ORDER BY id LIMIT 1)",
    )
    with pytest.raises(asyncpg.RaiseError, match="rows of customers changed since the DRY RUN"):
        await _apply(conn, _draft(pins))
    # a delete plus an insert of the same count moves the ids: the pin sees it too
    pins = await _pins(conn)
    await _touch(conn, "UPDATE booking.payment_schedule SET id = gen_random_uuid() WHERE seq = 1")
    with pytest.raises(asyncpg.RaiseError, match="rows of payment_schedule changed since the DRY RUN"):
        await _apply(conn, _draft(pins))
    repinned = await _pins(conn)
    assert repinned["payment_schedule"]["n"] == pins["payment_schedule"]["n"]
    # known limit (no personal column is hashed): a hand edit of a personal column alone, triggers off, keeps the pin;
    # an app write moves updated_at (set_updated_at), which the pin and the 30-day guard both see
    await _touch(
        conn,
        "UPDATE booking.customers SET name = 'Invented Traveller B'"
        " WHERE id = (SELECT id FROM booking.customers ORDER BY id LIMIT 1)",
    )
    assert (await _pins(conn))["customers"] == repinned["customers"]


@pytest.mark.parametrize("sql", FRESH_KEPT_WRITES)
async def test_draft_refuses_fresh_writes_on_kept_b2c_rows(world: dict[str, Any], sql: str) -> None:
    conn = world["conn"]
    await _touch(conn, sql)
    try:
        with pytest.raises(asyncpg.RaiseError, match="less than 30 days ago"):
            await _apply(conn, _draft(await _pins(conn)))
    finally:
        await _backdate(conn)


async def test_draft_refuses_while_the_intake_runs(world: dict[str, Any]) -> None:
    conn = world["conn"]
    job = await conn.fetchval(
        "INSERT INTO booking.job (kind, payload, status, run_after) VALUES ('bk_inquiry_pull', '{}'::jsonb, 'queued',"
        " now()) RETURNING id"
    )
    try:
        with pytest.raises(asyncpg.RaiseError, match="the inquiry intake still runs"):
            await _apply(conn, _draft(await _pins(conn)))
    finally:
        await conn.execute("DELETE FROM booking.job WHERE id = $1", job)


async def test_dry_run_is_ready_once_frozen(world: dict[str, Any]) -> None:
    dry = _dry_run()
    report = await dry.measure(world["conn"])
    assert {k: v["n"] for k, v in report["pins"].items()} == EXPECTED_ROWS
    assert report["kept"] == EXPECTED_KEPT
    verdict = dry.verdict(report)
    assert verdict["ready"] is True, verdict
    assert verdict["reasons"] == []
    # bk-035 granted the inquiry view to cis_app; the DROP removes the grant, its reader must be gone first
    assert verdict["notes"] == [
        "booking.v_ops_inquiry_inbox is granted to cis_app SELECT: confirm its reader is removed"
    ]


async def test_draft_archives_money_drops_b2c_tables_and_keeps_history(world: dict[str, Any]) -> None:
    conn = world["conn"]
    pins = await _pins(conn)
    money: dict[str, dict[str, Any]] = {}
    async with conn.transaction():
        await conn.execute("SET LOCAL TimeZone = 'UTC'")  # the migration writes the archive in UTC
        for t in MONEY_TABLES:
            rows = await conn.fetch(f"SELECT id::text AS id, to_jsonb(t) AS r FROM booking.{t} t")  # noqa: S608
            money[t] = {r["id"]: json.loads(r["r"]) for r in rows}
    await _apply(conn, _draft(pins))

    for rel in B2C_TABLES:
        assert await conn.fetchval("SELECT to_regclass($1)", rel) is None, rel
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM pg_attribute WHERE attrelid = 'booking.trip_cases'::regclass"
            " AND attname = 'customer_id' AND NOT attisdropped"
        )
        == 0
    )

    # every customer money row is archived whole, kept 10 years after the year of its last write
    for table, rows in money.items():
        archived = await conn.fetch(
            "SELECT source_id::text AS id, record, last_write_at, retain_until, archived_by"
            " FROM booking.retired_b2c_record WHERE source_table = $1",
            table,
        )
        assert {r["id"]: json.loads(r["record"]) for r in archived} == rows, table
        for r in archived:
            assert (r["retain_until"].month, r["retain_until"].day) == (12, 31)
            assert r["retain_until"].year == r["last_write_at"].year + 10
            assert r["archived_by"] == VERSION
    assert sum(len(rows) for rows in money.values()) == 5
    assert (
        await conn.fetchval("SELECT has_table_privilege('booking', 'booking.retired_b2c_record', 'SELECT') AS ok")
        is False
    )
    assert (
        await conn.fetchval("SELECT has_table_privilege('cis_app', 'booking.retired_b2c_record', 'SELECT') AS ok")
        is False
    )

    # the b2c case graph and every row about it stay (DM-7.1: supplier money, files, audit, status history, inbox)
    assert await conn.fetchval("SELECT count(*) FROM booking.trip_cases WHERE channel = 'b2c'") == 2
    assert await conn.fetchval(f"SELECT count(*) FROM ({LAN_BILL}) b") == 1  # noqa: S608
    assert await conn.fetchval(f"SELECT count(*) FROM booking.supplier_payouts WHERE bill_id IN ({LAN_BILL})") == 1  # noqa: S608
    assert await conn.fetchval(f"SELECT count(*) FROM booking.supplier_bill_items WHERE bill_id IN ({LAN_BILL})") == 1  # noqa: S608
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM booking.supplier_bookings WHERE trip_case_id = $1::uuid", world["sold"]["id"]
        )
        == 1
    )
    assert await conn.fetchval("SELECT count(*) FROM booking.ops_files WHERE kind = 'payment_evidence'") == 1
    assert await conn.fetchval("SELECT count(*) FROM booking.inbox_events WHERE contract = 'inquiry.submitted'") == 1
    assert await conn.fetchval("SELECT count(*) FROM booking.audit_logs WHERE entity_type = 'customer'") == 1
    assert (
        await conn.fetchval("SELECT count(*) FROM booking.audit_logs WHERE entity_id = $1::uuid", world["lead"]["id"])
        == 1
    )
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM booking.trip_case_status_history WHERE trip_case_id = $1::uuid", world["sold"]["id"]
        )
        >= 1
    )
    # B2B rows and the CIS feed stay
    assert await conn.fetchval("SELECT count(*) FROM booking.trip_cases WHERE id = $1::uuid", world["b2b"]["id"]) == 1
    assert (
        await conn.fetchval("SELECT count(*) FROM booking.supplier_bookings WHERE id = $1::uuid", world["b2b_booking"])
        == 1
    )
    assert await conn.fetchval("SELECT count(*) FROM booking.inbox_events WHERE idempotency_key = $1", FEED_REF) == 1
    assert (
        await conn.fetchval("SELECT count(*) FROM booking.audit_logs WHERE entity_id = $1::uuid", world["b2b"]["id"])
        == 1
    )
    # agency billing and the CIS feed keep their objects
    for rel in (
        "booking.invoice_sequence",
        "booking.agency_invoice",
        "booking.agency_payment",
        "booking.inbox_cursor",
        "booking.v_tenant_billing_status",
        "booking.v_trip_check_board",
        "booking.v_ops_data_totals",
        "booking.v_ops_funnel_daily",
    ):
        assert await conn.fetchval("SELECT to_regclass($1)", rel) is not None, rel
    assert await conn.fetchval("SELECT count(*) FROM booking.ops_transitions WHERE entity = 'invoice'") == 0
    assert await conn.fetchval("SELECT count(*) FROM booking.v_ops_data_totals WHERE metric = 'customers'") == 0
    assert await conn.fetchval("SELECT version FROM shared.schema_versions WHERE version = $1", VERSION) == VERSION

    # a kept b2c case is still redactable (DM-6.7); a new b2c case is refused
    await conn.execute(
        "UPDATE booking.trip_cases SET notes = 'it-682-1037-redacted' WHERE id = $1::uuid", world["lead"]["id"]
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO booking.trip_cases (channel, status, currency, notes) VALUES ('b2c', 'new_inquiry', 'USD',"
            " 'it-682-1037-b2c-after')"
        )
    # customer payment evidence is frozen, and no new one comes in
    with pytest.raises(asyncpg.CheckViolationError, match="retired customer payment"):
        await conn.execute(
            "UPDATE booking.ops_files SET status = 'removed', removed_at = now(), removed_reason = 'it-1037',"
            " removed_by = $1 WHERE entity_type = 'payment'",
            STAFF_A,
        )
    new_file = str(uuid.uuid4())
    with pytest.raises(asyncpg.CheckViolationError, match="unknown entity type"):
        await conn.execute(
            "INSERT INTO booking.ops_files (id, kind, entity_type, entity_id, trip_case_id, s3_bucket, s3_key,"
            " content_type, bytes, sha256, file_name, uploaded_by) VALUES ($1::uuid, 'payment_evidence', 'payment',"
            " $2::uuid, $3::uuid, $4, $5, 'application/pdf', 10, $6, 'slip.pdf', $7)",
            new_file,
            str(uuid.uuid4()),
            world["sold"]["id"],
            BUCKET,
            f"ops-docs/payment_evidence/{new_file}.pdf",
            "b" * 64,
            STAFF_A,
        )
    # the money edges are gone, so the edge check refuses first; the rewritten guards name no dropped table
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM pg_proc WHERE pronamespace = 'booking'::regnamespace"
            " AND prosrc ~ 'booking\\.(invoices|payments|payment_schedule|invoice_items|customers|customer_[a-z]+"
            "|media_files)\\M'"
            " AND proname <> 'agency_invoice_guard'"
        )
        == 0
    )
    with pytest.raises(asyncpg.CheckViolationError, match="illegal trip case status transition"):
        await conn.execute(
            "UPDATE booking.trip_cases SET status = 'invoice_sent' WHERE id = $1::uuid", world["b2b"]["id"]
        )
    assert (
        await conn.fetchval(
            "SELECT payment_verified IS NULL AS ok FROM booking.v_trip_check_board WHERE trip_case_id = $1::uuid",
            world["b2b"]["id"],
        )
        is True
    )

    # the role booking can still read what the app reads
    for rel in ("booking.v_trip_check_board", "booking.v_ops_data_totals", "booking.trip_cases"):
        assert await conn.fetchval("SELECT has_table_privilege('booking', $1, 'SELECT') AS ok", rel) is True, rel
    assert await conn.fetchval("SELECT has_table_privilege('cis_app', 'booking.v_ops_data_totals', 'SELECT') AS ok")

    # re-runnable: the second run changes nothing and archives nothing new
    constraint = await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'trip_cases_channel_chk'"
    )
    await _apply(conn, _draft(pins))
    assert await conn.fetchval("SELECT count(*) FROM booking.trip_cases WHERE id = $1::uuid", world["b2b"]["id"]) == 1
    assert await conn.fetchval("SELECT count(*) FROM booking.retired_b2c_record") == 5
    assert (
        await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'trip_cases_channel_chk'"
        )
        == constraint
    )
