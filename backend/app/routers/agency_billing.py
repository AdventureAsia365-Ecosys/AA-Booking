"""Agency billing for staff (AA-1037 R4 slice 1, root ADR 0019 d9; ``app.agency_billing``).

Read ``agency_billing.read``, write ``agency_billing.write``, approve / reject a payment ``agency_payment.approve``
(never the recorder: 403 ``maker_checker``), configuration ``billing_config.write``: admin and finance (the finance role
of bk-038). Not in preview sessions; the E2E session stays read-only (global rule in ``app.auth.staff``).

- ``GET /agency-invoices`` (``kind``, ``status`` draft|issued|paid|void|overdue|waiting|all, ``tenant_id``, ``q``) ·
  ``GET /agency-invoices/summary`` · ``GET /agency-invoices/{ref}`` (id or number)
- ``PATCH /agency-invoices/{ref}`` (``staff_note``; ``e_invoice_no`` once on an issued invoice) · ``DELETE`` (a draft)
- ``POST /agency-invoices/{ref}/lines`` (an adjustment on a draft) · ``DELETE /agency-invoices/{ref}/lines/{n}``
- ``POST /agency-invoices/{ref}/issue`` (``due_date`` optional) · ``POST /agency-invoices/{ref}/void`` (``reason``)
- ``POST /agency-invoices/{ref}/payments`` (maker; ``Idempotency-Key``) · ``GET /agency-payments`` (``state``,
  ``invoice``) · ``POST /agency-payments/{id}/approve`` · ``POST /agency-payments/{id}/reject`` (checker)
- ``GET /agency-billing/status`` (the billing status of every agency, ``booking.v_tenant_billing_status``)
- ``POST /agency-billing/runs`` (queue a draft job) · ``GET /agency-billing/runs``
- ``GET|POST /billing-entities``, ``PATCH /billing-entities/{id}`` · ``GET|POST /tax-rules``, ``PATCH /tax-rules/{id}``
- ``GET /billing-terms`` · ``PUT /billing-terms/default`` · ``PUT|DELETE /billing-terms/agencies/{tenant_id}``
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Header, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.agency_billing import config, drafts, invoices, payments
from app.auth.staff import Staff, get_pool, require

router = APIRouter(prefix="/booking", tags=["agency-billing"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("agency_billing.read"))]
CanWrite = Annotated[Staff, Depends(require("agency_billing.write"))]
CanApprove = Annotated[Staff, Depends(require("agency_payment.approve"))]
CanConfigure = Annotated[Staff, Depends(require("billing_config.write"))]
Ref = Annotated[str, Path(min_length=1, max_length=64, description="Invoice UUID or number (AAS-2026-000001)")]
IdempotencyKey = Annotated[
    str | None, Header(alias="Idempotency-Key", max_length=200, description="8-200 characters, one per request")
]
Money = Annotated[Decimal, Field(max_digits=14, decimal_places=2)]
PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_-]{1,31}$")]
Country = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Reason = Annotated[str, Field(min_length=1, max_length=500)]
Kind = Literal["saas", "settlement"]
Period = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]


class AgencyInvoiceChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    staff_note: str | None = Field(default=None, max_length=2000)
    e_invoice_no: str | None = Field(default=None, min_length=1, max_length=64)


class AgencyLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=300)
    quantity: Annotated[Decimal, Field(gt=0, le=9999, max_digits=10, decimal_places=2)] = Decimal(1)
    unit_price: Money


class AgencyIssueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    due_date: date | None = None


class AgencyReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class AgencyPaymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: PositiveMoney
    method: Literal["bank_transfer", "cash", "card_offline", "other"]
    received_on: date
    reference: str | None = Field(default=None, min_length=1, max_length=100)
    document_ref: str = Field(min_length=1, max_length=300, description="the evidence: statement line, slip, file id")
    note: str | None = Field(default=None, min_length=1, max_length=2000)


class AgencyDraftRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    period: Period | None = None
    tenant_id: UUID | None = None


class BillingEntityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Code
    legal_entity: str = Field(min_length=1, max_length=300)
    tax_code: str | None = Field(default=None, min_length=1, max_length=64)
    registered_office: str | None = Field(default=None, min_length=1, max_length=1000)
    country_iso2: Country | None = None
    invoice_inbox: str | None = Field(default=None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")
    payment_instructions: Text | None = None
    is_default: bool = False


class BillingEntityChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    legal_entity: str | None = Field(default=None, min_length=1, max_length=300)
    tax_code: str | None = Field(default=None, min_length=1, max_length=64)
    registered_office: str | None = Field(default=None, min_length=1, max_length=1000)
    country_iso2: Country | None = None
    invoice_inbox: str | None = Field(default=None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")
    payment_instructions: Text | None = None
    is_default: bool | None = None
    active: bool | None = None


class TaxRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Code
    invoice_kind: Kind | None = None
    recipient_country: Country | None = None
    rate: Annotated[Decimal, Field(ge=0, lt=1, max_digits=6, decimal_places=4)]
    line_wording: str = Field(min_length=1, max_length=120)
    footer_wording: str | None = Field(default=None, min_length=1, max_length=1000)
    effective_from: date
    effective_to: date | None = None


class TaxRuleChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_kind: Kind | None = None
    recipient_country: Country | None = None
    rate: Annotated[Decimal, Field(ge=0, lt=1, max_digits=6, decimal_places=4)] | None = None
    line_wording: str | None = Field(default=None, min_length=1, max_length=120)
    footer_wording: str | None = Field(default=None, min_length=1, max_length=1000)
    effective_from: date | None = None
    effective_to: date | None = None
    active: bool | None = None


class BillingTermsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deposit_pct: Annotated[Decimal, Field(ge=0, le=1, max_digits=5, decimal_places=4)]
    balance_days_before_departure: int = Field(ge=0, le=365)
    saas_due_days: int = Field(ge=0, le=120)
    settlement_due_days: int = Field(ge=0, le=120)
    warn_after_days: int = Field(ge=0, le=365)
    suspend_after_days: int = Field(ge=1, le=365)
    saas_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


# ── Invoices ─────────────────────────────────────────────────────────────────


@router.get("/agency-invoices")
async def list_agency_invoices(
    _: CanRead,
    pool: Pool,
    kind: Annotated[Kind | None, Query()] = None,
    status: Annotated[Literal["draft", "issued", "paid", "void", "overdue", "waiting", "all"] | None, Query()] = None,
    tenant_id: Annotated[UUID | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100, description="number, agency or e-invoice number")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await invoices.list_invoices(
            conn,
            kind=kind,
            status=status,
            tenant_id=str(tenant_id) if tenant_id else None,
            q=q,
            limit=limit,
            offset=offset,
        )


@router.get("/agency-invoices/summary")
async def agency_invoice_summary(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await invoices.summary(conn)


@router.get("/agency-invoices/{ref}")
async def get_agency_invoice(ref: Ref, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await invoices.get(conn, ref)


@router.patch("/agency-invoices/{ref}")
async def patch_agency_invoice(ref: Ref, body: AgencyInvoiceChanges, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await invoices.patch(conn, ref, body.model_dump(exclude_unset=True), staff.cis_user_id)


@router.delete("/agency-invoices/{ref}", status_code=204)
async def delete_agency_invoice(ref: Ref, staff: CanWrite, pool: Pool) -> Response:
    async with pool.acquire() as conn:
        await invoices.delete_draft(conn, ref, staff.cis_user_id)
    return Response(status_code=204)


@router.post("/agency-invoices/{ref}/lines", status_code=201)
async def add_agency_invoice_line(ref: Ref, body: AgencyLineIn, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await invoices.add_line(conn, ref, body.model_dump(), staff.cis_user_id)


@router.delete("/agency-invoices/{ref}/lines/{line_no}")
async def remove_agency_invoice_line(
    ref: Ref, line_no: Annotated[int, Path(ge=1, le=500)], staff: CanWrite, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await invoices.remove_line(conn, ref, line_no, staff.cis_user_id)


@router.post("/agency-invoices/{ref}/issue")
async def issue_agency_invoice(
    ref: Ref, staff: CanWrite, pool: Pool, body: AgencyIssueIn | None = None
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await invoices.issue(conn, ref, staff.cis_user_id, due_date=body.due_date if body else None)


@router.post("/agency-invoices/{ref}/void")
async def void_agency_invoice(ref: Ref, body: AgencyReasonIn, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await invoices.void(conn, ref, body.reason, staff.cis_user_id)


# ── Payments ─────────────────────────────────────────────────────────────────


@router.post("/agency-invoices/{ref}/payments", status_code=201)
async def record_agency_payment(
    ref: Ref, body: AgencyPaymentIn, staff: CanWrite, pool: Pool, response: Response, key: IdempotencyKey = None
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        out = await payments.record(conn, ref, body.model_dump(), staff.cis_user_id, key)
    if out["replayed"]:
        response.status_code = 200
    return out


@router.get("/agency-payments")
async def list_agency_payments(
    _: CanRead,
    pool: Pool,
    state: Annotated[Literal["recorded", "approved", "rejected", "all"] | None, Query()] = None,
    invoice: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await payments.list_payments(conn, state=state, invoice=invoice, limit=limit, offset=offset)


@router.post("/agency-payments/{payment_id}/approve")
async def approve_agency_payment(payment_id: UUID, staff: CanApprove, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await payments.approve(conn, str(payment_id), staff.cis_user_id)


@router.post("/agency-payments/{payment_id}/reject")
async def reject_agency_payment(
    payment_id: UUID, body: AgencyReasonIn, staff: CanApprove, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await payments.reject(conn, str(payment_id), body.reason, staff.cis_user_id)


# ── Status and runs ──────────────────────────────────────────────────────────

STATUS_SQL = """
SELECT s.tenant_id::text AS tenant_id, p.agency_name, p.status AS partner_status, s.status, s.oldest_overdue_days,
       s.overdue_invoices, s.overdue_amount, s.paid_through, s.warn_after_days, s.suspend_after_days
  FROM booking.v_tenant_billing_status s
  JOIN booking.b2b_partners p ON p.tenant_id = s.tenant_id
 ORDER BY CASE s.status WHEN 'past_due' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, s.oldest_overdue_days DESC,
          p.agency_name
"""


@router.get("/agency-billing/status")
async def agency_billing_status(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(STATUS_SQL)]
    return {"items": rows}


@router.post("/agency-billing/runs", status_code=202)
async def run_agency_drafts(body: AgencyDraftRunIn, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    return await drafts.enqueue_run(
        pool, body.kind, body.period, str(body.tenant_id) if body.tenant_id else None, f"staff:{staff.cis_user_id}"
    )


@router.get("/agency-billing/runs")
async def agency_draft_runs(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return {"items": await drafts.recent_runs(conn)}


# ── Configuration ────────────────────────────────────────────────────────────


@router.get("/billing-entities")
async def list_billing_entities(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return {"items": await config.list_entities(conn)}


@router.post("/billing-entities", status_code=201)
async def create_billing_entity(body: BillingEntityIn, staff: CanConfigure, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await config.create_entity(conn, body.model_dump(), staff.cis_user_id)


@router.patch("/billing-entities/{entity_id}")
async def update_billing_entity(entity_id: UUID, body: BillingEntityChanges, staff: CanConfigure, pool: Pool
                                ) -> dict[str, Any]:  # fmt: skip
    async with pool.acquire() as conn:
        return await config.update_entity(conn, str(entity_id), body.model_dump(exclude_unset=True), staff.cis_user_id)


@router.get("/tax-rules")
async def list_tax_rules(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return {"items": await config.list_tax_rules(conn)}


@router.post("/tax-rules", status_code=201)
async def create_tax_rule(body: TaxRuleIn, staff: CanConfigure, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await config.create_tax_rule(conn, body.model_dump(), staff.cis_user_id)


@router.patch("/tax-rules/{rule_id}")
async def update_tax_rule(rule_id: UUID, body: TaxRuleChanges, staff: CanConfigure, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await config.update_tax_rule(conn, str(rule_id), body.model_dump(exclude_unset=True),
                                            staff.cis_user_id)  # fmt: skip


@router.get("/billing-terms")
async def list_billing_terms(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return {"items": await config.list_terms(conn)}


@router.put("/billing-terms/default")
async def put_default_terms(body: BillingTermsIn, staff: CanConfigure, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await config.put_terms(conn, None, body.model_dump(), staff.cis_user_id)


@router.put("/billing-terms/agencies/{tenant_id}")
async def put_agency_terms(tenant_id: UUID, body: BillingTermsIn, staff: CanConfigure, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await config.put_terms(conn, str(tenant_id), body.model_dump(), staff.cis_user_id)


@router.delete("/billing-terms/agencies/{tenant_id}", status_code=204)
async def delete_agency_terms(tenant_id: UUID, staff: CanConfigure, pool: Pool) -> Response:
    async with pool.acquire() as conn:
        await config.delete_terms_override(conn, str(tenant_id), staff.cis_user_id)
    return Response(status_code=204)
