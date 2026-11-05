"""AA-1037 (booking.b2b.v1 1.5) on a real PostgreSQL, as role ``booking``, through the HTTP API: an agency reads its
own AA invoices and billing status, and nothing of another agency's.

Two agencies on the same CRM client (one ``aa-m2m`` client serves every agency; the partner comes from
``X-AA-Tenant-Id``): A has an issued settlement invoice with a staff note and an approved part payment (bank reference,
document, maker and checker), a paid software-fee invoice, a void invoice with its reason and a draft; B has one issued
invoice. A lists its three numbered invoices only (never the draft), filters by kind and status, reads its detail with
the approved payment and the issuer's payment instructions and none of the staff fields; A asking for B's number, or B
for A's, is 404 ``invoice_not_found``; the billing status shows A's paid-through day. Names and places are invented."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal as D
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from tests.integration.test_aa1037_g_extras import _purge_billing
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client are used by name
    TENANT,
    _insert,
    _m2m,
    _ok,
    _problem,
    admin,
    client,
)
from tests.staff_tokens import CRM_CLIENT, LocalCis, LocalM2M

FIXTURES = (admin, client)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]
TENANT_B = "e2000000-0000-4000-8000-000000001037"
TODAY = date.today()
STAFF_TEXT = ("Call the owner before the next run", "Issued twice by mistake", "TRX-7781", "bank statement 4",
              "it-1037b-fin-a", "it-1037b-fin-b", "accounts@juniper.example")  # fmt: skip
ISSUER = {"code": "ITGXAB", "legal_entity": "Lantern Bay Travel Ltd", "invoice_inbox": "billing@lanternbay.example",
          "payment_instructions": "Transfer to the account on the contract.", "tax_line": "VAT 0%"}  # fmt: skip
BILL_TO = {"agency_name": "Juniper Ridge", "source": "cis", "legal_entity_name": "Juniper Ridge Travel Co",
           "billing_email": "accounts@juniper.example"}  # fmt: skip


@pytest_asyncio.fixture
async def agencies(admin: asyncpg.Connection) -> AsyncIterator[dict[str, Any]]:  # noqa: F811
    await _purge_billing(admin)
    a = await _insert(admin, "b2b_partners", tenant_id=TENANT, agency_name="Juniper Ridge", m2m_client_id=CRM_CLIENT)
    b = await _insert(admin, "b2b_partners", tenant_id=TENANT_B, agency_name="Saffron Coast", m2m_client_id=CRM_CLIENT)
    entity = await admin.fetchval(
        "INSERT INTO booking.billing_entity (code, legal_entity, invoice_inbox, payment_instructions)"
        " VALUES ('ITGXAB', 'Lantern Bay Travel Ltd', 'billing@lanternbay.example',"
        " 'Transfer to the account on the contract.') RETURNING id"
    )
    try:
        yield {"a": a, "b": b, "entity": entity}
    finally:
        await _purge_billing(admin)


async def _invoice(admin: asyncpg.Connection, entity: Any, tenant: str, kind: str, start: date, total: str, *,
                   number: str | None, note: str | None = None) -> str:  # fmt: skip
    inv = await admin.fetchval(
        "INSERT INTO booking.agency_invoice (party_tenant_id, invoice_kind, period_start, period_end, currency,"
        " subtotal, total, source, created_by, idempotency_key, staff_note)"
        " VALUES ($1::uuid, $2, $3, $4, 'USD', $5, $5, 'job', 'job:test', $6, $7) RETURNING id::text",
        tenant,
        kind,
        start,
        start + timedelta(days=30),
        D(total),
        f"{kind}:{tenant}:{start}:USD",
        note,
    )
    await admin.execute(
        "INSERT INTO booking.agency_invoice_line (invoice_id, invoice_kind, line_no, line_role, description,"
        " unit_price, amount, created_by) VALUES ($1::uuid, $2, 1, 'adjustment', 'Agreed amount', $3, $3, 'job:test')",
        inv,
        kind,
        D(total),
    )
    if number is not None:
        await admin.execute(
            "UPDATE booking.agency_invoice SET status = 'issued', number = $2, issued_at = now(), issued_by = $3,"
            " issued_on = $4, due_date = $5, billing_entity_id = $6, issuer = $7::jsonb, bill_to = $8::jsonb"
            " WHERE id = $1::uuid",
            inv,
            number,
            "it-1037b-fin-a",
            TODAY,
            TODAY + timedelta(days=14),
            entity,
            json.dumps(ISSUER),
            json.dumps(BILL_TO),
        )
    return inv


async def _approved_payment(admin: asyncpg.Connection, inv: str, amount: str) -> None:
    pay = await admin.fetchval(
        "INSERT INTO booking.agency_payment (invoice_id, amount, currency, method, received_on, reference,"
        " document_ref, note, recorded_by, idempotency_key) VALUES ($1::uuid, $2, 'USD', 'bank_transfer', $3,"
        " 'TRX-7781', 'bank statement 4', 'Call the owner before the next run', 'it-1037b-fin-a', $4) RETURNING id",
        inv,
        D(amount),
        TODAY,
        f"pay-{inv[:8]}",
    )
    await admin.execute(
        "UPDATE booking.agency_payment SET status = 'approved', approved_by = 'it-1037b-fin-b', approved_at = now()"
        " WHERE id = $1",
        pay,
    )


def _as_b(m2m: LocalM2M) -> dict[str, str]:
    return {"Authorization": f"Bearer {m2m.token()}", "X-AA-Tenant-Id": TENANT_B}


def _clean(body: Any) -> None:
    text = json.dumps(body)
    for word in (*STAFF_TEXT, "staff_note", "void_reason", "bill_to", "recorded_by", "approved_by", "document_ref",
                 "reference", "waiting", "Juniper Ridge Travel Co"):  # fmt: skip
        assert word not in text, word


async def test_an_agency_reads_its_own_invoices_only(
    client: Client,  # noqa: F811
    admin: asyncpg.Connection,  # noqa: F811
    agencies: dict[str, Any],
) -> None:
    c, _, m2m, _ = client
    entity = agencies["entity"]
    open_inv = await _invoice(admin, entity, TENANT, "settlement", date(2026, 9, 1), "500.00", number="AAT-2026-000951",
                              note="Call the owner before the next run")  # fmt: skip
    await _approved_payment(admin, open_inv, "200.00")
    paid = await _invoice(admin, entity, TENANT, "saas", date(2026, 10, 1), "299.00", number="AAS-2026-000952")
    await _approved_payment(admin, paid, "299.00")
    void = await _invoice(admin, entity, TENANT, "settlement", date(2026, 10, 1), "80.00", number="AAT-2026-000953")
    await admin.execute(
        "UPDATE booking.agency_invoice SET status = 'void', voided_at = now(), voided_by = 'it-1037b-fin-b',"
        " void_reason = 'Issued twice by mistake' WHERE id = $1::uuid",
        void,
    )
    draft = await _invoice(admin, entity, TENANT, "settlement", date(2026, 11, 1), "40.00", number=None)
    await _invoice(admin, entity, TENANT_B, "settlement", date(2026, 9, 1), "900.00", number="AAT-2026-000954")
    assert await admin.fetchval("SELECT status FROM booking.agency_invoice WHERE id = $1::uuid", paid) == "paid"

    page = await _ok(await c.get("/b2b/v1/invoices", headers=_m2m(m2m)))
    assert page["total"] == 3 and page["limit"] == 50 and page["offset"] == 0
    assert {i["number"] for i in page["items"]} == {"AAT-2026-000951", "AAS-2026-000952", "AAT-2026-000953"}
    _clean(page)
    saas = await _ok(await c.get("/b2b/v1/invoices", params={"kind": "saas"}, headers=_m2m(m2m)))
    assert [(i["number"], i["status"], i["balance"]) for i in saas["items"]] == [("AAS-2026-000952", "paid", "0.00")]
    voided = await _ok(await c.get("/b2b/v1/invoices", params={"status": "void"}, headers=_m2m(m2m)))
    assert [i["number"] for i in voided["items"]] == ["AAT-2026-000953"]
    one = await _ok(await c.get("/b2b/v1/invoices", params={"limit": 1, "offset": 1}, headers=_m2m(m2m)))
    assert one["total"] == 3 and len(one["items"]) == 1
    _problem(await c.get("/b2b/v1/invoices", params={"status": "draft"}, headers=_m2m(m2m)), 422, "invalid")

    doc = await _ok(await c.get("/b2b/v1/invoices/AAT-2026-000951", headers=_m2m(m2m)))
    _clean(doc)
    assert (doc["status"], doc["total"], doc["paid"], doc["balance"]) == ("issued", "500.00", "200.00", "300.00")
    assert doc["period_end"] == "2026-09-30" and doc["due_date"] == (TODAY + timedelta(days=14)).isoformat()
    assert doc["payments"] == [{"received_on": TODAY.isoformat(), "amount": "200.00", "currency": "USD"}]
    assert doc["issuer"]["payment_instructions"] == "Transfer to the account on the contract."
    assert [(ln["line_role"], ln["amount"], ln["hold_id"]) for ln in doc["lines"]] == [("adjustment", "500.00", None)]
    void_doc = await _ok(await c.get("/b2b/v1/invoices/AAT-2026-000953", headers=_m2m(m2m)))
    assert void_doc["status"] == "void" and void_doc["balance"] == "0.00"
    _clean(void_doc)

    # Another agency's number, a draft (by its id) and junk: the same 404 for A.
    for ref in ("AAT-2026-000954", draft, "AAT-2026-999999", "not-a-number"):
        _problem(await c.get(f"/b2b/v1/invoices/{ref}", headers=_m2m(m2m)), 404, "invoice_not_found")
    # And B sees its own invoice, not A's.
    mine = await _ok(await c.get("/b2b/v1/invoices", headers=_as_b(m2m)))
    assert [i["number"] for i in mine["items"]] == ["AAT-2026-000954"]
    _problem(await c.get("/b2b/v1/invoices/AAT-2026-000951", headers=_as_b(m2m)), 404, "invoice_not_found")
    assert (await _ok(await c.get("/b2b/v1/invoices/AAT-2026-000954", headers=_as_b(m2m))))["total"] == "900.00"

    status = await _ok(await c.get("/b2b/v1/billing-status", headers=_m2m(m2m)))
    assert status["status"] == "ok" and status["holds_paused"] is False
    assert status["paid_through"] == "2026-10-30"  # the paid fee covers 1-30 Oct (period_end is exclusive)
    other = await _ok(await c.get("/b2b/v1/billing-status", headers=_as_b(m2m)))
    assert other["paid_through"] is None

    # A suspended agency reads nothing.
    await admin.execute("UPDATE booking.b2b_partners SET status = 'suspended' WHERE tenant_id = $1::uuid", TENANT_B)
    _problem(await c.get("/b2b/v1/invoices", headers=_as_b(m2m)), 403, "unknown_partner")
