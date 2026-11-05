"""AA-1037 R4 slice 1 on a real PostgreSQL (bk-041 / bk-042), as role ``booking``: the slice's Done-when end to end.

- SaaS: the draft job reads the CIS subscriptions (``catalog.v_tenant_billing_v1``, CIS 377, stood in by a table of
  its exact columns around each test) and writes one draft per agency and month; a re-run changes nothing; issue gives
  the next AAS- number with the CIS bill-to snapshot; the e-invoice number is recorded once; finance A records a
  payment (Idempotency-Key replay), A approving is 403 ``maker_checker``, finance B approves -> paid, and the billing
  status shows the paid-through day;
- settlement: a confirmed B2B hold far from departure gives a deposit (30 % from the default terms), a re-run nothing,
  the month of the balance date the balance; a hold close to departure is billed in full; void with a waiting payment
  is 409, after the rejection it voids and the next run drafts the lines again;
- configuration: terms validation, an agency override, a country tax rule applied at issue;
- permissions: sales reads no agency billing.

Names and places are invented."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.agency_billing import cis as cis_view
from app.agency_billing import drafts
from app.catalog import liveness
from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.integration import ops
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FIN_A, FIN_B, SALES = "it-1037-fin-a", "it-1037-fin-b", "it-1037-sales"
CIS_ROLE = {FIN_A: "reviewer", FIN_B: "reviewer", SALES: "reviewer"}  # the row's role is kept (bridge never sets it)
CLIENT = "aa1037client"
NAME = "it-1037"
# Copied verbatim from the closing check of CIS api/migrations/381_tenant_billing_catalog_view.sql.
CIS_377_COLUMNS = (
    "tenant_id uuid, name text, kind text, legal_entity_name text, tax_id text, billing_email text, "
    "billing_address text, billing_country text, product text, plan_key text, plan_name text, "
    "price_usd_monthly numeric(10,2), price_unit text, seats_included integer, price_extra_seat_usd numeric(10,2), "
    "seats integer, subscription_status text"
)
COLUMNS_SQL = """
SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum)
FROM pg_attribute a WHERE a.attrelid = $1::regclass AND a.attnum > 0 AND NOT a.attisdropped
"""
BILLING_TABLES = ("booking.agency_payment", "booking.agency_invoice_line", "booking.agency_invoice")

Client = tuple[httpx.AsyncClient, LocalCis]


async def purge(conn: asyncpg.Connection) -> None:
    """Every agency billing row and the partners / quotes / holds / cases of these tests (the guards are switched off
    for the purge only, as bk_owner, like ``ops.purge_ops``)."""
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE bk_owner")
        for t in BILLING_TABLES:
            await conn.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
        for t in BILLING_TABLES:
            await conn.execute(f"DELETE FROM {t}")  # noqa: S608
        for t in BILLING_TABLES:
            await conn.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")
        await conn.execute("DELETE FROM booking.billing_terms WHERE party_tenant_id IS NOT NULL")
        await conn.execute("DELETE FROM booking.tax_rule WHERE code LIKE 'IT1037%'")
        await conn.execute("UPDATE booking.billing_entity SET is_default = false WHERE code LIKE 'IT1037%'")
        await conn.execute("DELETE FROM booking.billing_entity WHERE code LIKE 'IT1037%'")
        await conn.execute("DELETE FROM booking.invoice_sequence")
        partners = "SELECT id FROM booking.b2b_partners WHERE agency_name LIKE 'it-1037%'"
        await conn.execute(f"DELETE FROM booking.b2b_holds WHERE partner_id IN ({partners})")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.b2b_quotes WHERE partner_id IN ({partners})")  # noqa: S608
        cases = "SELECT id FROM booking.trip_cases WHERE notes LIKE 'it-1037%'"
        await conn.execute(f"DELETE FROM booking.trip_case_status_history WHERE trip_case_id IN ({cases})")  # noqa: S608
        await conn.execute("DELETE FROM booking.trip_cases WHERE notes LIKE 'it-1037%'")
        await conn.execute("DELETE FROM booking.b2b_partners WHERE agency_name LIKE 'it-1037%'")
        await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type IN ('agency_invoice', 'agency_payment',"
                           " 'billing_entity', 'tax_rule', 'billing_terms')")  # fmt: skip
    await conn.execute("DROP TABLE IF EXISTS catalog.v_tenant_billing_v1")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await purge(conn)
    await ops.staff(conn, (FIN_A, "finance"), (FIN_B, "finance"), (SALES, "sales"))
    await conn.execute(f"CREATE TABLE catalog.v_tenant_billing_v1 ({CIS_377_COLUMNS})")
    await conn.execute("GRANT SELECT ON catalog.v_tenant_billing_v1 TO booking")
    assert await conn.fetchval(COLUMNS_SQL, cis_view.VIEW.qualified) == CIS_377_COLUMNS
    missing = set(cis_view.COLUMNS) - {c.split(" ", 1)[0] for c in CIS_377_COLUMNS.split(", ")}
    assert not missing, f"Booking selects {missing} that CIS 377 does not have"
    liveness.reset_cache()
    try:
        yield conn
    finally:
        await purge(conn)
        liveness.reset_cache()
        await conn.close()


@pytest_asyncio.fixture
async def client(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[Client]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis = Database(settings), LocalCis()
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


def _as(cis: LocalCis, sub: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {cis.token(sub=sub, cis_role=CIS_ROLE[sub])}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


async def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _err(response: httpx.Response, status: int, detail: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["detail"] == detail, response.json()


async def _partner(admin: asyncpg.Connection, label: str) -> tuple[str, str]:
    tid = str(uuid.uuid4())
    pid = await admin.fetchval(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
        " VALUES ($1::uuid, $2, 'active', 'USD', $3) RETURNING id::text",
        tid,
        f"{NAME} {label}",
        CLIENT,
    )
    return tid, pid


async def _subscription(admin: asyncpg.Connection, tid: str, product: str, plan: str, price: str | None, *,
                        unit: str = "tenant", seats: int | None = None, included: int | None = None,
                        extra: str | None = None) -> None:  # fmt: skip
    await admin.execute(
        "INSERT INTO catalog.v_tenant_billing_v1 VALUES ($1::uuid, 'Juniper Ridge', 'demo', 'Juniper Ridge Travel Co',"
        " 'TX-0042', 'accounts@juniper.example', '12 Lantern Street, Hillbrook', 'TH', $2, $3, $4, $5, $6, $7, $8,"
        " $9, 'active')",
        tid,
        product,
        plan,
        plan.split(".")[1].title(),
        Decimal(price) if price else None,
        unit,
        included,
        Decimal(extra) if extra else None,
        seats,
    )


async def _entity(c: httpx.AsyncClient, cis: LocalCis) -> dict[str, Any]:
    body = {"code": "IT1037AA", "legal_entity": "Lantern Bay Travel Ltd", "tax_code": "0100000001",
            "country_iso2": "VN", "payment_instructions": "Transfer to the account on the contract.",
            "is_default": True}  # fmt: skip
    return await _ok(await c.post("/billing-entities", json=body, headers=_as(cis, FIN_A)), 201)


async def _hold(admin: asyncpg.Connection, pid: str, tid: str, *, total: str, confirmed: datetime, departs: date,
                tag: str) -> str:  # fmt: skip
    case = await admin.fetchval(
        "INSERT INTO booking.trip_cases (channel, party_tenant_id, status, start_date, end_date, pax, currency,"
        " source_system, notes) VALUES ('b2b', $1::uuid, 'confirmed', $2, $3, 2, 'USD', 'crm', $4) RETURNING id::text",
        tid,
        departs,
        departs + timedelta(days=5),
        f"{NAME}-{tag}",
    )
    quote = await admin.fetchval(
        "INSERT INTO booking.b2b_quotes (partner_id, m2m_client_id, proposal_ref, proposal_version, currency,"
        " fx_rate_per_usd, fx_buffer_pct, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,"
        " rounding, priced_on, total_b2b_price, price_hash, valid_until, created_at)"
        " VALUES ($1::uuid, $2, $3, 1, 'USD', 1, 0, $4, 'test', $4, 0.2, 0, 'none', $4, $5, $6, $7, $8)"
        " RETURNING id::text",
        pid,
        CLIENT,
        f"PR-{tag}",
        confirmed.date(),
        Decimal(total),
        "a" * 64,
        confirmed + timedelta(days=2),
        confirmed - timedelta(hours=2),
    )
    return await admin.fetchval(
        "INSERT INTO booking.b2b_holds (partner_id, m2m_client_id, quote_id, proposal_ref, proposal_version,"
        " idempotency_key, status, expires_at, trip_case_id, confirmed_at, created_at)"
        " VALUES ($1::uuid, $2, $3::uuid, $4, 1, $5, 'confirmed', $6, $7::uuid, $8, $9) RETURNING id::text",
        pid,
        CLIENT,
        quote,
        f"PR-{tag}",
        f"hold-{tag}-{uuid.uuid4().hex[:8]}",
        confirmed + timedelta(days=2),
        case,
        confirmed,
        confirmed - timedelta(hours=1),
    )


async def _lines(admin: asyncpg.Connection, tid: str, kind: str) -> list[asyncpg.Record]:
    return await admin.fetch(
        "SELECT i.period_start, i.status, l.line_role, l.amount FROM booking.agency_invoice_line l"
        " JOIN booking.agency_invoice i ON i.id = l.invoice_id WHERE i.party_tenant_id = $1::uuid"
        " AND i.invoice_kind = $2 AND NOT l.voided ORDER BY i.period_start, l.line_no",
        tid,
        kind,
    )


async def test_saas_done_when(client: Client, admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    c, cis = client
    tid, _ = await _partner(admin, "Juniper Ridge")
    await _subscription(admin, tid, "content", "content.growth", "799.00")
    await _subscription(admin, tid, "crm", "crm.team", "30.00", unit="seat", seats=3)
    await _entity(c, cis)

    first = await drafts.run_saas(pool, period="2026-11", tenant_id=tid, actor="job:test")
    assert (first["invoices_created"], first["lines_added"]) == (1, 2)
    again = await drafts.run_saas(pool, period="2026-11", tenant_id=tid, actor="job:test")
    assert (again["invoices_created"], again["drafts_updated"]) == (0, 1)
    page = await _ok(await c.get("/agency-invoices", params={"tenant_id": tid}, headers=_as(cis, FIN_A)))
    assert page["total"] == 1 and page["items"][0]["status"] == "draft"
    inv = page["items"][0]
    assert Decimal(inv["total"]) == Decimal("889.00")  # 799 + 3 x 30, prices exclude tax (seed rule 0 %)

    issued = await _ok(await c.post(f"/agency-invoices/{inv['id']}/issue", headers=_as(cis, FIN_A)))
    assert re.fullmatch(rf"AAS-{datetime.now(UTC).year}-000001", issued["number"])
    assert issued["bill_to"]["source"] == "cis" and issued["bill_to"]["tax_id"] == "TX-0042"
    assert issued["issuer"]["legal_entity"] == "Lantern Bay Travel Ltd"
    assert issued["footer_wording"].startswith("Prices exclude tax")
    _err(await c.patch(f"/agency-invoices/{inv['id']}", json={"staff_note": "x"}, headers=_as(cis, SALES)), 403,
         "forbidden")  # fmt: skip
    number = issued["number"]
    done = await _ok(await c.patch(f"/agency-invoices/{number}", json={"e_invoice_no": "C26TLB-00012"},
                                   headers=_as(cis, FIN_A)))  # fmt: skip
    assert done["e_invoice_no"] == "C26TLB-00012"
    _err(await c.patch(f"/agency-invoices/{number}", json={"e_invoice_no": "again"}, headers=_as(cis, FIN_A)), 409,
         "e_invoice_no_set")  # fmt: skip

    body = {"amount": "889.00", "method": "bank_transfer", "received_on": date.today().isoformat(),
            "reference": "TRX 77", "document_ref": "statement 2026-11 line 4"}  # fmt: skip
    paid = await _ok(await c.post(f"/agency-invoices/{number}/payments", json=body,
                                  headers=_as(cis, FIN_A, "pay-it1037-1")), 201)  # fmt: skip
    again = await _ok(await c.post(f"/agency-invoices/{number}/payments", json=body,
                                   headers=_as(cis, FIN_A, "pay-it1037-1")))  # fmt: skip
    assert again["replayed"] is True and again["id"] == paid["id"]
    _err(await c.post(f"/agency-payments/{paid['id']}/approve", headers=_as(cis, FIN_A)), 403, "maker_checker")
    ok = await _ok(await c.post(f"/agency-payments/{paid['id']}/approve", headers=_as(cis, FIN_B)))
    assert ok["status"] == "approved" and ok["invoice_status"] == "paid"
    status = await _ok(await c.get("/agency-billing/status", headers=_as(cis, FIN_B)))
    mine = next(s for s in status["items"] if s["tenant_id"] == tid)
    assert mine["status"] == "ok" and mine["paid_through"] == "2026-11-30"
    _err(await c.get("/agency-invoices", headers=_as(cis, SALES)), 403, "forbidden")


async def test_saas_price_missing_and_extra_seats(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    tid, _ = await _partner(admin, "Saffron Coast")
    await _subscription(admin, tid, "content", "content.enterprise", None)
    await _subscription(admin, tid, "tripplanner", "tripplanner.site", "99.00", seats=4, included=2, extra="10.00")
    out = await drafts.run_saas(pool, period="2026-11", tenant_id=tid, actor="job:test")
    assert out["price_missing"] == 1 and out["lines_added"] == 3
    rows = await _lines(admin, tid, "saas")
    assert [(r["line_role"], r["amount"]) for r in rows] == [
        ("subscription", Decimal("0.00")),
        ("subscription", Decimal("99.00")),
        ("extra_seats", Decimal("20.00")),
    ]


async def test_settlement_terms_and_void(client: Client, admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    c, cis = client
    await _entity(c, cis)
    tid, pid = await _partner(admin, "Marigold Bay")
    await _hold(admin, pid, tid, total="1000.00", confirmed=datetime(2026, 9, 10, 8, tzinfo=UTC),
                departs=date(2026, 12, 20), tag="far")  # fmt: skip
    await _hold(admin, pid, tid, total="400.00", confirmed=datetime(2026, 9, 25, 8, tzinfo=UTC),
                departs=date(2026, 10, 15), tag="near")  # fmt: skip

    sep = await drafts.run_settlement(pool, period="2026-09", tenant_id=tid, actor="job:test")
    assert (sep["invoices_created"], sep["lines_added"]) == (1, 2)
    assert [(r["line_role"], r["amount"]) for r in await _lines(admin, tid, "settlement")] == [
        ("deposit", Decimal("300.00")),
        ("full", Decimal("400.00")),
    ]
    again = await drafts.run_settlement(pool, period="2026-09", tenant_id=tid, actor="job:test")
    assert again["lines_added"] == 0
    assert (await drafts.run_settlement(pool, period="2026-10", tenant_id=tid, actor="job:test"))["lines_added"] == 0
    nov = await drafts.run_settlement(pool, period="2026-11", tenant_id=tid, actor="job:test")  # balance due 11-20
    assert nov["lines_added"] == 1
    assert (await _lines(admin, tid, "settlement"))[-1]["line_role"] == "balance"
    assert (await _lines(admin, tid, "settlement"))[-1]["amount"] == Decimal("700.00")

    sep_inv = await admin.fetchval(
        "SELECT id::text FROM booking.agency_invoice WHERE party_tenant_id = $1::uuid AND period_start = '2026-09-01'",
        tid,
    )
    issued = await _ok(await c.post(f"/agency-invoices/{sep_inv}/issue", json={"due_date": date.today().isoformat()},
                                    headers=_as(cis, FIN_A)))  # fmt: skip
    assert issued["number"].startswith("AAT-") and Decimal(issued["total"]) == Decimal("700.00")
    _err(await c.post(f"/agency-invoices/{sep_inv}/payments", json={"amount": "800.00", "method": "cash",
         "received_on": date.today().isoformat(), "document_ref": "receipt 9"},
         headers=_as(cis, FIN_A, "pay-it1037-over")), 409, "amount_exceeds_balance")  # fmt: skip
    waiting = await _ok(await c.post(f"/agency-invoices/{sep_inv}/payments", json={"amount": "100.00",
                        "method": "cash", "received_on": date.today().isoformat(), "document_ref": "receipt 10"},
                        headers=_as(cis, FIN_A, "pay-it1037-2")), 201)  # fmt: skip
    _err(await c.post(f"/agency-invoices/{sep_inv}/void", json={"reason": "wrong departure"},
                      headers=_as(cis, FIN_A)), 409, "payments_waiting")  # fmt: skip
    await _ok(await c.post(f"/agency-payments/{waiting['id']}/reject", json={"reason": "not on the statement"},
                           headers=_as(cis, FIN_B)))  # fmt: skip
    voided = await _ok(await c.post(f"/agency-invoices/{sep_inv}/void", json={"reason": "wrong departure"},
                                    headers=_as(cis, FIN_A)))  # fmt: skip
    assert voided["status"] == "void" and all(ln["voided"] for ln in voided["lines"])
    redo = await drafts.run_settlement(pool, period="2026-09", tenant_id=tid, actor="job:test")
    assert (redo["invoices_created"], redo["lines_added"]) == (1, 2)


async def test_configuration(client: Client, admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    c, cis = client
    await _entity(c, cis)
    tid, _ = await _partner(admin, "Driftwood Isles")
    terms = {"deposit_pct": "0.3000", "balance_days_before_departure": 30, "saas_due_days": 14,
             "settlement_due_days": 14, "warn_after_days": 10, "suspend_after_days": 5}  # fmt: skip
    _err(await c.put("/billing-terms/default", json=terms, headers=_as(cis, FIN_A)), 422, "invalid_terms")
    override = await _ok(await c.put(f"/billing-terms/agencies/{tid}", json={**terms, "suspend_after_days": 45},
                                     headers=_as(cis, FIN_A)))  # fmt: skip
    assert override["suspend_after_days"] == 45
    listed = await _ok(await c.get("/billing-terms", headers=_as(cis, FIN_B)))
    assert {t["agency_name"] for t in listed["items"]} >= {None, f"{NAME} Driftwood Isles"}
    rule = {"code": "IT1037TH", "invoice_kind": "saas", "recipient_country": "TH", "rate": "0.0700",
            "line_wording": "VAT 7%", "effective_from": "2026-01-01"}  # fmt: skip
    await _ok(await c.post("/tax-rules", json=rule, headers=_as(cis, FIN_A)), 201)
    await _subscription(admin, tid, "content", "content.starter", "299.00")
    await drafts.run_saas(pool, period="2026-11", tenant_id=tid, actor="job:test")
    inv = await admin.fetchval("SELECT id::text FROM booking.agency_invoice WHERE party_tenant_id = $1::uuid", tid)
    issued = await _ok(await c.post(f"/agency-invoices/{inv}/issue", headers=_as(cis, FIN_A)))
    assert (Decimal(issued["tax_amount"]), Decimal(issued["total"])) == (Decimal("20.93"), Decimal("319.93"))
    assert issued["issuer"]["tax_line"] == "VAT 7%"
    assert (await c.delete(f"/billing-terms/agencies/{tid}", headers=_as(cis, FIN_A))).status_code == 204
    run = await _ok(await c.post("/agency-billing/runs", json={"kind": "settlement", "period": "2026-10"},
                                 headers=_as(cis, FIN_A)), 202)  # fmt: skip
    assert run["kind"] == "bk_settlement_draft" and run["period"] == "2026-10"
    runs = await _ok(await c.get("/agency-billing/runs", headers=_as(cis, FIN_A)))
    assert runs["items"][0]["id"] == run["job_id"]
    await admin.execute("DELETE FROM booking.job WHERE id = $1::uuid", run["job_id"])
